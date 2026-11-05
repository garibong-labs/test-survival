"""Java/Maven adapter, ported from the recorded experiment's JDK AST runner."""
import collections
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import threading
from uuid import uuid4
import xml.etree.ElementTree as ET

from ..config import as_written, canonical_hash, record_runner_inputs
from ..io import read_json, write_json, write_csv, result_exists
from ..runner.docker import DockerRunner
from ..runner.cache import ClasspathCache
from .java_worker import RUNNER_JAR_RETRIES, RUNNER_JAR_BACKOFF_SECONDS, project_jars, runner_test_jars
from .maven_dependencies import resolution_timeout
from .run_reuse import survival_provenance_matches
from ..rules.survival import dead_method


_BUILD_LOCKS = {}
_BUILD_LOCKS_GUARD = threading.Lock()


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

    def build_fingerprint(self, sha, *, legacy=False):
        runner = as_written(self.config['runner'], include_defaults=True)
        if not legacy:
            runner = record_runner_inputs(runner)
        # Preserve pre-volume build records when the new cache default is implicit.
        if 'm2_volume' not in as_written(self.config['runner']['docker']):
            runner['docker'].pop('m2_volume', None)
        return canonical_hash({'sha': sha, 'runner': runner, 'module': self.config['project']['module']})

    def build_fingerprints(self, sha):
        """Current key first, then the pre-policy key of records that hashed operational settings."""
        return self.build_fingerprint(sha), self.build_fingerprint(sha, legacy=True)

    def build(self, index, sha):
        with _BUILD_LOCKS_GUARD:
            lock = _BUILD_LOCKS.setdefault((str(self.exp.resolve()), str(index)), threading.RLock())
        with lock:
            return self._build(index, sha)

    def dependency_identity(self, index, record, directory):
        directory.mkdir(parents=True, exist_ok=True)
        output = directory/'dependencies.json'
        log = directory/'dependencies.log'
        # Compose progress and stderr belong to diagnostics, never the payload.
        # Require this invocation to publish its own output, even on a retry.
        output.unlink(missing_ok=True)
        result = self.runner.execute(['python3', str(Path(__file__).with_name('workspace_identity.py')),
                                      str(self.exp/record['classpath']), str(output)], self.folder(index), log,
                                     self.config['runner']['timeouts']['build'])
        if result['exit_code'] or result.get('timeout') or not output.is_file():
            raise RuntimeError('Cannot verify workspace dependencies; see '+str(log))
        return read_json(output)

    def ensure_anchor(self):
        anchor = read_json(self.exp/'results/anchor.json', {})
        # Rejected/interrupted restorations can leave classes behind. Preserve
        # ready legacy trees, but never bypass build validation for unsettled work.
        unsettled = read_json(self.exp/'state/workspace-0.json', {}).get('status') in ('running', 'failed')
        if anchor.get('sha') and (unsettled or not (self.folder(0)/'target/classes').exists()):
            if self.build(0, anchor['sha'])['build'] != 'ok':
                raise RuntimeError('Anchor workspace build failed')

    def _build(self, index, sha):
        record = self.exp / f'results/builds/{index}.json'
        identity_path = self.exp/f'results/workspace-identities/{index}.json'
        fingerprint, legacy = self.build_fingerprints(sha)
        previous = read_json(record, {})
        recorded_identity = read_json(identity_path)
        compatible = previous.get('input_sha256') in (fingerprint, legacy) and previous.get('sha') == sha
        present = (self.folder(index)/'target/classes').exists()
        restoration_state = self.exp/f'state/workspace-{index}.json'
        unsettled = read_json(restoration_state, {}).get('status') in ('running', 'failed')
        if compatible and previous.get('build') == 'ok' and present and not unsettled:
            return previous
        # An identity does not make an invalidated build authoritative for new
        # inputs. Compare using the original SHA so a changed index mapping with
        # a recorded identity still fails the SHA check under unchanged settings.
        same_inputs = previous.get('input_sha256') in self.build_fingerprints(previous.get('sha'))
        restoration = previous.get('build') == 'ok' and (compatible or recorded_identity is not None and same_inputs)
        directory = self.exp/f'results/workspace-restorations/{index}/{uuid4().hex}' if restoration else self.exp/'results/builds/logs'
        if previous and not restoration:
            attempt = uuid4().hex
            history = self.exp/f'results/build-history/{index}/{attempt}'
            history.mkdir(parents=True)
            for source, name in ((record, 'build.json'), (identity_path, 'identity.json'),
                                 (restoration_state, 'workspace-state.json')):
                if source.is_file():
                    shutil.copy2(source, history/name)
            # Preserve all diagnostics referenced by superseded records, even
            # if this new attempt fails or is interrupted before publication.
            directory = self.exp/f'results/builds/logs/{index}/{attempt}'
        directory.mkdir(parents=True, exist_ok=True)
        if restoration and (previous.get('sha') != sha or recorded_identity and recorded_identity['sha'] != sha):
            write_json(directory/'result.json', dict(status='failed', reason='SHA disagrees', sha=sha))
            raise RuntimeError('Workspace restoration SHA disagrees with authoritative build')
        # For old layouts still containing their workspace, capture the dependency
        # identity before reconstruction. Without original identity, fail closed.
        if restoration and recorded_identity is None:
            if not present:
                raise RuntimeError('Cannot restore old workspace without recorded dependency identity')
            recorded_identity = dict(sha=sha, dependencies=self.dependency_identity(index, previous, directory))
            write_json(identity_path, recorded_identity)
        if restoration:
            write_json(restoration_state, dict(status='running', sha=sha, diagnostics=str(directory.relative_to(self.exp))))
        self.checkout(index, sha)
        folder = self.folder(index)
        try:
            try:
                digest, cache, runs = self.cache.resolve(folder, index, log_root=directory)
            except RuntimeError as exc:
                if restoration:
                    raise
                result = dict(index=index, sha=sha, build='fail', attempts=[], classpath_error=str(exc), input_sha256=fingerprint)
                write_json(record, result)
                restoration_state.unlink(missing_ok=True)
                return result
            attempts = []
            for number, offline in enumerate((True, False), 1):
                attempt = self.runner.execute(['mvn', '-q', *(['-o'] if offline else []), '-DskipTests', 'compile'], folder,
                                              directory/f'{index}-{number}.txt', self.config['runner']['timeouts']['build'])
                attempts.append(attempt)
                if attempt['exit_code'] == 0:
                    break
            result = dict(index=index, sha=sha, build='ok' if attempts[-1]['exit_code'] == 0 else 'fail', attempts=attempts, input_sha256=fingerprint)
            if result['build'] == 'ok':
                result.update(pom_sha256=digest, classpath=str(cache.relative_to(self.exp)), classpath_attempts=runs)
                observed = dict(sha=sha, dependencies=self.dependency_identity(index, result, directory/(str(index)+'-identity')))
                if restoration and observed != recorded_identity:
                    raise RuntimeError('Workspace restoration dependency identity disagrees')
                if not restoration:
                    write_json(identity_path, observed)
            if restoration:
                write_json(directory/'result.json', result)
                if result['build'] != previous['build']:
                    raise RuntimeError('Workspace restoration verdict disagrees')
                write_json(restoration_state, dict(status='completed', sha=sha, diagnostics=str(directory.relative_to(self.exp))))
                return previous
            write_json(record, result)
            restoration_state.unlink(missing_ok=True)
            return result
        except BaseException as exc:
            if restoration:
                write_json(restoration_state, dict(status='failed', sha=sha, diagnostics=str(directory.relative_to(self.exp))))
                write_json(directory/'failure.json', dict(error_type=type(exc).__name__, error=str(exc), sha=sha))
                raise
            raise

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
        exclude_unmapped = self.config['dev'].get('unmapped', 'error') == 'exclude'
        excluded = {}
        for repeat in range(1, self.config['dev']['stable_runs']+1):
            record_path = self.exp / f'results/baseline-run{repeat}.json'
            destination = self.exp / f'results/baseline-reports/run{repeat}'
            if not record_path.exists():
                self.cache.resolve(folder, 0)
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
                    if not candidates and case.find('skipped') is not None and not exclude_unmapped:
                        continue
                    if len(candidates) != 1:
                        unmapped.append(dict(run=repeat, **{'class': cls}, name=name))
                        reason = 'no-source-method' if not candidates else 'ambiguous-source-method'
                        entry = excluded.setdefault((cls, name), {'runs': set(), 'reasons': set()})
                        entry['runs'].add(repeat)
                        entry['reasons'].add(reason)
                        continue
                    state = 'error' if case.find('error') is not None else 'fail' if case.find('failure') is not None else 'skipped' if case.find('skipped') is not None else 'pass'
                    observed[(cls, candidates[0]['method'])].append(state)
            observations.append(observed)
        write_json(self.exp/'results/baseline-unmapped.json', unmapped)
        excluded_rows = [dict(zip(('class', 'name'), key), runs_seen=len(value['runs']),
                              reason=','.join(sorted(value['reasons'])))
                         for key, value in sorted(excluded.items())] if exclude_unmapped else []
        write_csv(self.exp/'dev/dev-unmapped-excluded.csv', excluded_rows, ['class', 'name', 'runs_seen', 'reason'])
        if unmapped and not exclude_unmapped:
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
        paths = [*sorted(self.helpers.glob('*')), Path(__file__), Path(__file__).with_name('java_worker.py'), Path(__file__).with_name('compilation.py'), Path(__file__).with_name('maven_dependencies.py'), Path(__file__).parents[1]/'runner/docker.py', Path(__file__).parents[1]/'runner/cache.py', Path(__file__).parents[1]/'rules/environment.py']
        paths.append(Path(__file__).with_name('compiler_session.py'))
        paths.append(Path(__file__).parents[1]/'io.py')
        paths.extend(Path(__file__).with_name(name) for name in ('policy_coverage.py','snapshot_compile.py','compile_events.py'))
        return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}

    def worker(self, job):
        self.ensure_anchor()
        if job.get('index', 0) != 0:
            if self.build(job['index'], job['sha'])['build'] != 'ok':
                raise RuntimeError('Worker workspace build failed')
        developer=job.get('group') in {g['name'] for g in self.config['dev']['groups']}
        framework = self.config['project']['test_framework'] if job.get('action') == 'setup' else self.test_framework() if developer else 'auto'
        job.update(experiment=self.runner.path(self.exp), module=self.config['project']['module'],
                   javac_mode=self.config['runner']['javac_mode'],
                   test_framework=framework, developer=developer, environment_errors=self.config['survival']['environment_errors'], jvm=self.config['runner']['jvm'], timeouts=self.config['runner']['timeouts'], flaky_rerun=self.config['survival']['flaky_rerun'])
        path = self.exp/job.get('work','work/runner-setup')/'job.json'
        # The worker clears its work directory; keep its input outside that directory.
        path = path.parent.with_name(path.parent.name+'-job.json')
        command = ['python3', str(Path(__file__).with_name('java_worker.py')), str(path)]
        if job['action'] in ('policy-cover', 'policy-survive') and os.environ.get('SURVIVAL_JVM_JACOCO'):
            # Operational launch evidence only: never mutate runner.jvm or any
            # result/hash inputs. env also works inside a reused Docker session.
            job['coverage_environment'] = {'SURVIVAL_JVM_JACOCO': os.environ['SURVIVAL_JVM_JACOCO']}
            command = ['env', 'SURVIVAL_JVM_JACOCO='+os.environ['SURVIVAL_JVM_JACOCO'], *command]
        if job.get('work'):
            from ..work import retain_previous_work
            retain_previous_work(self.exp, job['work'])
        write_json(path, job)
        # A stale classpath can require both dependency goals, each offline then online.
        dependency_timeout = resolution_timeout(self.config['runner']['timeouts']['build'])
        timeout = (self.config['runner']['timeouts']['compile'] + self.config['runner']['timeouts']['test_class'] * 8) * max(1,len(job.get('methods', []))) + dependency_timeout + 600
        if job['action'] == 'setup':
            # Allow every sequential jar its configured attempt timeout and backoffs.
            timeout = len(read_json(self.helpers/'classpath-artifacts.json')) * ((RUNNER_JAR_RETRIES+1)*self.config['runner']['timeouts']['build'] + RUNNER_JAR_RETRIES*RUNNER_JAR_BACKOFF_SECONDS) + dependency_timeout + 600
        result = self.runner.execute(command, self.folder(0), path.with_suffix('.log'), timeout)
        if result['exit_code']:
            raise RuntimeError('Java worker failed; see '+str(path.with_suffix('.log')))
        return read_json(self.exp/job['output']) if job.get('output') else result

    def setup_runner(self):
        self.ensure_anchor()
        record = read_json(self.exp/'results/runner-setup.json',{})
        jars = read_json(self.helpers/'classpath-artifacts.json')
        digest = hashlib.sha256((self.folder(0)/'pom.xml').read_bytes()).hexdigest()
        # Setup is the single-threaded gate before parallel workers; restore removed runner jars here, not in workers.
        present = all((self.exp/'work/libs'/r['filename']).is_file() for r in jars)
        if record.get('status') != 'completed' or record.get('jars') != jars or record.get('project_dependencies') != digest or not present:
            self.worker({'action':'setup'})

    def screen_fingerprint(self, source, repeats, *, provenance=None):
        return canonical_hash({'source':hashlib.sha256(source.read_bytes()).hexdigest(), 'repeats':repeats,
                               'runner':self.provenance() if provenance is None else provenance})

    def screen_fingerprints(self, source, repeats, *, provenance=None):
        from .run_reuse import pre_footprint_provenance
        provenance = self.provenance() if provenance is None else provenance
        return (self.screen_fingerprint(source, repeats, provenance=provenance),
                self.screen_fingerprint(source, repeats, provenance=pre_footprint_provenance(provenance)))

    def screen_file(self, group, round_name, source, repeats, number):
        relative = str(source.relative_to(self.exp))
        output = f'generated/{group}/{round_name}/validation/{number}.json'
        key, legacy_key = self.screen_fingerprints(source, repeats)
        old = read_json(self.exp/output,{})
        if old.get('input_sha256') in (key, legacy_key):
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
        _, cache, _ = self.cache.resolve(self.folder(index), index)
        # The worker's launcher classpath: manifest jars shadow the project's own JUnit and Mockito versions.
        jars = [self.runner.path(self.exp/'work/libs'/name) for name in runner_test_jars(read_json(self.helpers/'classpath-artifacts.json'))]
        cp = ':'.join([self.runner.path(self.folder(index)/'target/classes'), *jars, *project_jars(cache.read_text())])
        for number, source in enumerate(files):
            part = self.exp/f'work/compile/{index}/{number}'
            part.mkdir(parents=True,exist_ok=True)
            results.append(self.runner.execute(['javac','-J-Xmx'+self.config['runner']['jvm']['javac'],'-cp',cp,'-d',str(part),str(source)],self.folder(index),part/'javac.txt',self.config['runner']['timeouts']['compile']))
        return results

    def run_fingerprints(self, methods, repeats=1):
        """Current key first, then the pre-policy key of records that hashed operational settings."""
        inputs={'methods':[(r['id'],r.get('execution_hash') or hashlib.sha256((self.exp/r['file']).read_bytes()).hexdigest()) for r in methods],
                'project':self.config['project'], 'survival':self.config['survival'], 'repeats':repeats}
        return (canonical_hash({**inputs, 'runner':record_runner_inputs(self.config['runner'])}),
                canonical_hash({**inputs, 'runner':self.config['runner']}))

    def run_fingerprint(self, methods, repeats=1):
        return self.run_fingerprints(methods, repeats)[0]

    def run_group(self,index,group,methods,phase='A',repeats=1,*,dead_at=None):
        fingerprint,legacy=self.run_fingerprints(methods,repeats)
        provenance=self.provenance()
        folder='raw' if phase=='A' else 'raw-coverage' if phase=='B' else phase
        output=f'results/{folder}/{index}-{group}.json'
        old=read_json(self.exp/output,{})
        commit=read_json(self.exp/'results/anchor.json') if index==0 else read_json(self.exp/f'results/builds/{index}.json')
        compatible = survival_provenance_matches(old.get('runner_sha256'), provenance) and old.get('status')!='coverage-fail'
        complete = dead_at is None or {m['id'] for m in old.get('methods', [])} == {m['id'] for m in methods}
        # A changed skip policy can make an earlier failure unevaluated without
        # changing the full-list fingerprint. Omitted tests then need execution.
        omitted = {m['id'] for m in old.get('methods', []) if m.get('state') == 'dead'}
        complete = complete and omitted <= (dead_at or {}).keys()
        if old.get('input_sha256') in (fingerprint,legacy) and compatible and complete and old.get('sha')==commit['sha']:
            return old
        dead_at = dead_at or {}
        selected = [m for m in methods if m['id'] not in dead_at]
        return self.worker(dict(action='run',index=index,sha=commit['sha'],group=group,phase=phase,methods=selected,repeats=repeats,
                                dead_methods=[dead_method(m, dead_at[m['id']]) for m in methods if m['id'] in dead_at],
                                input_sha256=fingerprint,runner_sha256=provenance,output=output,work=f'work/phase-{phase}/{index}/{group}'))

    def method_coverage(self, method):
        name=method['class']+'#'+method['method']
        xml=f'results/dev-method-coverage-t/{name}.xml'
        output=f'results/dev-method-coverage-t/{name}.json'
        inputs={'method':method,'source':hashlib.sha256((self.exp/method['file']).read_bytes()).hexdigest(),
                'project':self.config['project'],'environment_errors':self.config['survival']['environment_errors']}
        fingerprint=canonical_hash({**inputs,'runner':record_runner_inputs(self.config['runner'])})
        legacy=canonical_hash({**inputs,'runner':self.config['runner']})
        previous=read_json(self.exp/output,{})
        provenance=self.provenance()
        anchor=read_json(self.exp/'results/anchor.json')
        if previous.get('input_sha256') in (fingerprint,legacy) and survival_provenance_matches(previous.get('runner_sha256'),provenance) and previous.get('sha')==anchor['sha'] and result_exists(self.exp/xml):return previous
        return self.worker(dict(action='run',index=0,sha=anchor['sha'],group=self.config['dev']['groups'][0]['name'],phase='B',methods=[method],repeats=1,retry=False,environment_retry=True,
                                input_sha256=fingerprint,runner_sha256=provenance,coverage_xml=xml,output=output,work='work/dev-method-coverage-t/'+name))

    def coverage(self,index,files,methods,*,group='dev'):
        return self.run_group(index,group,methods,'B')
