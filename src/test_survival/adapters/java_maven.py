"""Java/Maven adapter, ported from the recorded experiment's JDK AST runner."""
import collections
import hashlib
import io
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import xml.etree.ElementTree as ET

from ..config import canonical_hash
from ..io import read_json, write_json, write_csv
from ..runner.docker import DockerRunner
from ..runner.cache import ClasspathCache


class JavaMavenAdapter:
    def __init__(self, exp, config):
        self.exp, self.config = Path(exp), config
        self.repo = Path(config['project']['repo']).resolve()
        self.runner = DockerRunner(exp, config)
        self.helpers = Path(__file__).parent / 'java'
        self.cache = ClasspathCache(self.exp, config, self.runner)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    def folder(self, index):
        return self.exp / f'work/checkouts/{index}' / self.config['project']['module']

    def checkout(self, index, sha):
        target = self.exp / f'work/checkouts/{index}'
        marker = target.parent / f'{index}.json'
        if read_json(marker, {}).get('sha') == sha and target.is_dir():
            return target
        if target.exists():
            shutil.rmtree(target)
        reuse = self.config['runner']['reuse']['checkouts']
        source = Path(reuse) / str(index) if reuse else None
        seed_ok = False
        if source and source.is_dir():
            recorded = read_json(source.parent.parent / f'results/builds/{index}.json', {})
            if recorded.get('sha') == sha:
                seed_ok = True
            elif (source / '.git').exists():
                seed_ok = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() == sha
        if seed_ok:
            shutil.copytree(source, target, ignore=shutil.ignore_patterns('.git'))
        else:
            target.mkdir(parents=True, exist_ok=True)
            archive = subprocess.check_output(['git', '-C', str(self.repo), 'archive', sha])
            with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
                stream.extractall(target, filter='data')
        write_json(marker, {'sha': sha, 'seeded_from': str(source) if seed_ok else None})
        return target

    def build(self, index, sha):
        record = self.exp / f'results/builds/{index}.json'
        fingerprint = canonical_hash({'sha': sha, 'runner': self.config['runner'], 'module': self.config['project']['module']})
        previous = read_json(record, {})
        if previous.get('input_sha256') == fingerprint and previous.get('build') == 'ok' and (self.folder(index)/'target/classes').exists():
            return previous
        self.checkout(index, sha)
        folder = self.folder(index)
        attempts = []
        for number, offline in enumerate((True, False), 1):
            attempt = self.runner.execute(['mvn', '-q', *(['-o'] if offline else []), '-DskipTests', 'compile'], folder,
                                          self.exp / f'results/builds/logs/{index}-{number}.txt', self.config['runner']['timeouts']['build'])
            attempts.append(attempt)
            if attempt['exit_code'] == 0:
                break
        result = dict(index=index, sha=sha, build='ok' if attempts[-1]['exit_code'] == 0 else 'fail', attempts=attempts, input_sha256=fingerprint)
        if result['build'] == 'ok':
            digest, cache, runs = self.cache.resolve(folder, index)
            result.update(pom_sha256=digest, classpath=str(cache.relative_to(self.exp)), classpath_attempts=runs)
        write_json(record, result)
        return result

    def test_framework(self):
        requested=self.config['project']['test_framework']
        if requested!='auto':return requested
        build=read_json(self.exp/'results/builds/0.json',{})
        cp=(self.exp/build['classpath']).read_text() if build.get('classpath') else ''
        selected='junit4' if ('/junit/junit/' in cp or re.search(r'(?:^|[:/])junit-4[^:]*\.jar',cp)) and 'junit-jupiter-engine' not in cp else 'junit5'
        write_json(self.exp/'results/test-framework.json',dict(configured=requested,resolved=selected))
        return selected

    def inventory(self, root, log):
        if not Path(root).exists() or not list(Path(root).rglob('*.java')) and Path(root).suffix != '.java':
            return {'classes': {}, 'methods': []}
        run = self.runner.execute(['java', '-Xmx'+self.config['runner']['jvm']['launcher'], str(self.helpers/'SourceInventory.java'), str(root), self.test_framework()],
                                  self.folder(0), log, self.config['runner']['timeouts']['compile'])
        if run['exit_code']:
            raise RuntimeError('Source inventory failed; see '+str(log))
        classes, methods = {}, []
        for line in Path(log).read_text().splitlines():
            parts = line.split('\t')
            if parts[0] == 'C':
                _, cls, kind, top, display, path = parts
                container_root = self.runner.path(self.folder(0))+'/'
                classes[cls] = dict(kind=kind, top=top=='true', display=display, path=path.removeprefix(container_root))
            elif parts[0] == 'M':
                _, cls, name, kind, eligible, signature, display, lineno, body_hash = parts
                methods.append(dict(**{'class': cls}, method=name, kind=kind, eligible=eligible=='true', signature=signature,
                                    display=display, line=int(lineno), body_hash=body_hash))
        return {'classes': classes, 'methods': methods}

    def list_production_methods(self, index):
        return self.inventory(self.folder(index)/'src/main/java', self.exp / f'results/source-inventory-main-{index}.tsv')

    def list_test_methods(self, files):
        return self.inventory(files, self.exp/'results/source-inventory-test.tsv')

    def baseline(self):
        folder = self.folder(0)
        frozen = self.exp/'dev/src-test-t'
        if not frozen.exists():
            shutil.copytree(folder/'src/test', frozen)
        inventory = {'main': self.list_production_methods(0), 'test': self.list_test_methods(frozen/'java')}
        # Inventory paths for frozen tests are normalized to their anchor source location.
        for data in inventory['test']['classes'].values():
            prefix = self.runner.path(frozen)+'/'
            data['path'] = data['path'].replace(prefix, 'src/test/')
        write_json(self.exp/'results/source-inventory.json', inventory)
        tests = [r for r in inventory['test']['methods'] if r['kind']]
        byclass = collections.defaultdict(list)
        for row in tests:
            byclass[row['class']].append(row)
        observations, unmapped = [], []
        for repeat in range(1, self.config['dev']['stable_runs']+1):
            record_path = self.exp / f'results/baseline-run{repeat}.json'
            destination = self.exp / f'results/baseline-reports/run{repeat}'
            if not record_path.exists():
                reports = folder/'target/surefire-reports'
                if reports.exists():
                    shutil.rmtree(reports)
                attempts = []
                for number, offline in enumerate((True, False), 1):
                    result = self.runner.execute(['mvn', '-q', *(['-o'] if offline else []), 'test'], folder,
                                                 self.exp/f'results/baseline-run{repeat}-{number}.txt', self.config['runner']['timeouts']['build'])
                    attempts.append(result)
                    if result['exit_code'] == 0 or reports.exists():
                        break
                if reports.exists():
                    shutil.copytree(reports, destination, dirs_exist_ok=True)
                write_json(record_path, dict(index=repeat, attempts=attempts))
            observed = collections.defaultdict(list)
            for path in sorted(destination.glob('TEST-*.xml')):
                cls = path.name[5:-4]
                for case in ET.parse(path).getroot().iter('testcase'):
                    name = case.get('name', '')
                    candidates = [m for m in byclass[cls] if re.match(re.escape(m['method'])+r'(?:\(|\[|$)', name)]
                    if not candidates:
                        candidates = [m for m in byclass[cls] if m['display'] and m['display'] in name]
                        if candidates:
                            longest = max(len(m['display']) for m in candidates)
                            candidates = [m for m in candidates if len(m['display']) == longest]
                    if not candidates and case.find('skipped') is not None:
                        continue
                    if len(candidates) != 1:
                        unmapped.append(dict(run=repeat, **{'class': cls}, name=name))
                        continue
                    state = 'error' if case.find('error') is not None else 'fail' if case.find('failure') is not None else 'skipped' if case.find('skipped') is not None else 'pass'
                    observed[(cls, candidates[0]['method'])].append(state)
            observations.append(observed)
        write_json(self.exp/'results/baseline-unmapped.json', unmapped)
        if unmapped:
            raise RuntimeError('Baseline cases cannot be mapped unambiguously to source methods')
        rows = []
        for method in tests:
            row = {k: method[k] for k in ('class', 'method', 'kind')}
            for repeat, observation in enumerate(observations, 1):
                states = observation.get((row['class'], row['method']), [])
                row[f'run{repeat}'] = 'pass' if states and set(states)=={'pass'} else ','.join(sorted(set(states))) if states else 'absent'
            row['stable'] = 'yes' if all(row[f'run{i}']=='pass' for i in range(1, len(observations)+1)) else 'no'
            rows.append(row)
        write_csv(self.exp/'dev/dev-methods-all.csv', rows, ['class','method','kind','stable',*[f'run{i}' for i in range(1, len(observations)+1)]])
        return rows

    def provenance(self):
        paths = [*sorted(self.helpers.glob('*')), Path(__file__), Path(__file__).with_name('java_worker.py'), Path(__file__).parents[1]/'runner/docker.py', Path(__file__).parents[1]/'runner/cache.py', Path(__file__).parents[1]/'rules/environment.py']
        return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}

    def worker(self, job):
        developer=job.get('group') in {g['name'] for g in self.config['dev']['groups']}
        job.update(experiment=self.runner.path(self.exp), module=self.config['project']['module'],
                   test_framework=self.test_framework() if developer else 'auto', developer=developer, environment_errors=self.config['survival']['environment_errors'], jvm=self.config['runner']['jvm'], timeouts=self.config['runner']['timeouts'], flaky_rerun=self.config['survival']['flaky_rerun'])
        path = self.exp/job.get('work','work/runner-setup')/'job.json'
        # The worker clears its work directory; keep its input outside that directory.
        path = path.parent.with_name(path.parent.name+'-job.json')
        write_json(path, job)
        timeout = (self.config['runner']['timeouts']['compile'] + self.config['runner']['timeouts']['test_class'] * 8) * max(1,len(job.get('methods', []))) + 600
        result = self.runner.execute(['python3', str(Path(__file__).with_name('java_worker.py')), str(path)], self.folder(0), path.with_suffix('.log'), timeout)
        if result['exit_code']:
            raise RuntimeError('Java worker failed; see '+str(path.with_suffix('.log')))
        return read_json(self.exp/job['output']) if job.get('output') else result

    def setup_runner(self):
        record = read_json(self.exp/'results/runner-setup.json',{})
        if record.get('status') != 'completed' or record.get('jars') != read_json(self.helpers/'classpath-artifacts.json'):
            self.worker({'action':'setup'})

    def screen_file(self, group, round_name, source, repeats, number):
        relative = str(source.relative_to(self.exp))
        output = f'generated/{group}/{round_name}/validation/{number}.json'
        key = canonical_hash({'source':hashlib.sha256(source.read_bytes()).hexdigest(),'repeats':repeats,'runner':self.provenance()})
        old = read_json(self.exp/output,{})
        if old.get('input_sha256') == key:
            return old
        result = self.worker(dict(action='screen',group=group,round=round_name,file=relative,
                                  technique='KTester' if next(g for g in self.config['generators'] if g['name']==group)['method']=='ktester' else source.parent.name,
                                  repeats=repeats,work=f'work/validation/{group}/{round_name}/{number}',output=output))
        result['input_sha256'] = key
        write_json(self.exp/output,result)
        return result

    def run_tests(self, index, files, methods, repeats=1, *, group='dev', phase='A'):
        return self.run_group(index,group,methods,phase,repeats)

    def compile_tests(self, index, files):
        results = []
        for number, source in enumerate(files):
            part = self.exp/f'work/compile/{index}/{number}'
            part.mkdir(parents=True,exist_ok=True)
            build = read_json(self.exp/f'results/builds/{index}.json')
            cp = self.runner.path(self.folder(index)/'target/classes')+':'+(self.exp/build['classpath']).read_text().strip()
            cp += ':'+':'.join(self.runner.path(p) for p in sorted((self.exp/'work/libs').glob('*.jar')))
            results.append(self.runner.execute(['javac','-J-Xmx'+self.config['runner']['jvm']['javac'],'-cp',cp,'-d',str(part),str(source)],self.folder(index),part/'javac.txt',self.config['runner']['timeouts']['compile']))
        return results

    def run_fingerprint(self, methods, repeats=1):
        return canonical_hash({'methods':[(r['id'],hashlib.sha256((self.exp/r['file']).read_bytes()).hexdigest()) for r in methods],
                               'project':self.config['project'], 'survival':self.config['survival'], 'runner':self.config['runner'], 'repeats':repeats})

    def run_group(self,index,group,methods,phase='A',repeats=1):
        fingerprint=self.run_fingerprint(methods,repeats)
        provenance=self.provenance()
        folder='raw' if phase=='A' else 'raw-coverage' if phase=='B' else phase
        output=f'results/{folder}/{index}-{group}.json'
        old=read_json(self.exp/output,{})
        commit=read_json(self.exp/'results/anchor.json') if index==0 else read_json(self.exp/f'results/builds/{index}.json')
        if old.get('input_sha256')==fingerprint and old.get('runner_sha256')==provenance and old.get('sha')==commit['sha']:
            return old
        return self.worker(dict(action='run',index=index,sha=commit['sha'],group=group,phase=phase,methods=methods,repeats=repeats,
                                input_sha256=fingerprint,runner_sha256=provenance,output=output,work=f'work/phase-{phase}/{index}/{group}'))

    def method_coverage(self, method):
        name=method['class']+'#'+method['method']
        xml=f'results/dev-method-coverage-t/{name}.xml'
        output=f'results/dev-method-coverage-t/{name}.json'
        fingerprint=canonical_hash({'method':method,'source':hashlib.sha256((self.exp/method['file']).read_bytes()).hexdigest(),
                                    'project':self.config['project'],'runner':self.config['runner'],
                                    'environment_errors':self.config['survival']['environment_errors']})
        previous=read_json(self.exp/output,{})
        provenance=self.provenance()
        anchor=read_json(self.exp/'results/anchor.json')
        if previous.get('input_sha256')==fingerprint and previous.get('runner_sha256')==provenance and previous.get('sha')==anchor['sha'] and (self.exp/xml).exists():return previous
        return self.worker(dict(action='run',index=0,sha=anchor['sha'],group=self.config['dev']['groups'][0]['name'],phase='B',methods=[method],repeats=1,retry=False,environment_retry=True,
                                input_sha256=fingerprint,runner_sha256=provenance,coverage_xml=xml,output=output,work='work/dev-method-coverage-t/'+name))

    def coverage(self,index,files,methods,*,group='dev'):
        return self.run_group(index,group,methods,'B')
