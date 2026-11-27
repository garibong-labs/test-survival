"""Method-level TestPilot refinement using the existing chat and screening ledgers."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import cache
from hashlib import sha256
from importlib.resources import files
from itertools import combinations
import re
import time

from .base import RoundResult
from .parts import context, import_hints, testpilot_materials
from .parts.extraction import extract, lexical_masks
from ..adapters.java_maven import JavaMavenAdapter
from ..config import canonical_hash, generator_config, runner_inputs
from ..io import atomic_text, now, read_csv, read_json, write_json
from ..model.openrouter import OpenRouter
from ..rules.naming import folder_keys


OPTIONS = ('body', 'doc', 'usage')


def subsets(available):
    for size in range(len(available)+1):
        yield from combinations(available, size)


def label(parts):
    return '-'.join(('sig', *parts))


@cache
def template(kind):
    return files('test_survival.generators.parts').joinpath('templates', f'testpilot-{kind}.txt').read_text()


def exact_text(path):
    # Preserve model and source CRLFs when replaying or verifying a request.
    return path.read_bytes().decode('utf-8')


def material_prompt(row, parts, hints, name):
    lines = []
    if 'usage' in parts:
        for number, snippet in enumerate(row['snippets'], 1):
            lines.extend([f'usage #{number}', *snippet['text'].splitlines()])
    if 'doc' in parts:
        lines.extend(row['doc'].splitlines())
    lines.extend([row['class'], *row['header'].splitlines()])
    if 'body' in parts:
        lines.extend(row['definition'].splitlines())
    method = row['method']
    scaffold = (hints.rstrip()+'\n\n'+''.join('// '+line+'\n' for line in lines)
                +f'public class {name} {{\n\n    @Test\n    public void test{method[:1].upper()+method[1:]}() {{')
    return template('material').format(display=row['class']+'#'+row['signature'], scaffold=scaffold)


def retry_prompt(source, error, roots):
    for root, relative in sorted(roots.items(), key=lambda item: -len(item[0])):
        error = error.replace(root.rstrip('/')+'/', relative)
    return template('retry').format(source=source.removesuffix('\n'), error=error[:4000].replace('\n', '\n// '))


def validation_id(source, directory):
    # Sorted ordinals move as concurrent methods publish retries or return no test.
    return sha256(source.relative_to(directory/'sources').as_posix().encode()).hexdigest()


class CandidateScreen:
    """Commit the inputs before the worker atomically publishes its single-file result."""
    def __init__(self, adapter, directory, group):
        self.adapter, self.directory, self.group = adapter, directory, group
        self.repeats = adapter.config['screen']['stable_runs']
        self.provenance = adapter.provenance()
        exp = adapter.exp
        self.environment = canonical_hash(dict(
            anchor=read_json(exp/'results/anchor.json')['sha'],
            workspace=read_json(exp/'results/workspace-identities/0.json'),
            resources=read_json(exp/'dev/frozen-test-tree.json', {}).get('resource_tree_hash'),
            runner=runner_inputs(adapter.config['runner'])))

    def run(self, source):
        slot = validation_id(source, self.directory)
        result_path = self.directory/'validation'/(slot+'.json')
        inputs_path = self.directory/'validation-inputs'/(slot+'.json')
        fingerprint = self.adapter.screen_fingerprint(source, self.repeats, provenance=self.provenance)
        inputs = dict(file=str(source.relative_to(self.adapter.exp)), environment=self.environment,
                      input_sha256=fingerprint)
        prior = read_json(inputs_path)
        result = read_json(result_path)
        matching = (prior == inputs and result is not None and result.get('file') == inputs['file']
                    and result.get('input_sha256', fingerprint) == fingerprint)
        if not matching:
            if result is not None:
                archive = self.directory/'validation-history'/slot/(canonical_hash(result)+'.json')
                archive.parent.mkdir(parents=True, exist_ok=True)
                result_path.replace(archive)
            if prior != inputs:
                write_json(inputs_path, inputs)
            result = self.adapter.screen_file(self.group, self.directory.name, source, self.repeats, slot)
        if 'testpilot_bodies' not in result:
            # Also seals an atomic worker result if the host stopped before
            # screen_file could publish its fingerprint.
            result['input_sha256'] = fingerprint
            hashes = {m['body_hash'] for m in result['methods']}
            result['testpilot_bodies'] = comment_free_bodies(source.read_text(), hashes)
            if not result.get('compile') or result['compile']['exit_code']:
                evidence = result.get('compile') or result['parse']
                result['diagnostic'] = (self.adapter.exp/evidence['log']).read_text(errors='replace')[:4000]
            write_json(result_path, result)
        return result


def outcome(record):
    compiled = record.get('compile')
    if not compiled or compiled['exit_code']:
        return 'compile-fail', record['diagnostic'], False
    for method in record['methods']:
        if method['state'] != 'pass':
            return method['state'], method['message'], True
    if not record['methods']:
        return 'absent', 'Method not reported by launcher', True
    return 'pass', '', True


def comment_free_bodies(source, hashes):
    """Map the runner's original body hashes to hashes with Java comments masked."""
    comments, code = lexical_masks(source, text_blocks=True)
    stack, result = [], {}
    def digest(body):
        return sha256(re.sub(r'\s', '', body, flags=re.ASCII).encode()).hexdigest()
    for index, char in enumerate(code):
        if char == '{':
            stack.append(index+1)
        elif char == '}' and stack:
            start = stack.pop()
            original = digest(source[start:index])
            if original in hashes:
                result[original] = digest(comments[start:index])
    return result


@dataclass(frozen=True)
class Candidate:
    key: str
    stem: str
    parts: tuple
    completion: int
    multiple: bool
    retry: bool = False

    @property
    def name(self):
        return self.stem+'S'+''.join(p[0].upper() for p in self.parts)+'Test'

    @property
    def id(self):
        suffix = f'_c{self.completion}' if self.multiple else ''
        return self.name.removesuffix('Test')+('R' if self.retry else '')+suffix

    @property
    def label(self):
        return label(self.parts)+('-retry' if self.retry else '')

    def source(self, directory):
        parent = directory/'sources'/self.key
        if self.multiple:
            parent = parent/f'c{self.completion}'
        return parent/self.label/(self.name+'.java')


class TestPilotNative:
    def __init__(self, exp, config, generator):
        self.exp, self.group = exp, generator
        self.adapter = JavaMavenAdapter(exp, generator_config(config, generator))
        # Use the same runner and inputs as the subsequent screen stage, including
        # when the group selected a different image for material extraction.
        self.screen_adapter = JavaMavenAdapter(exp, config)
        self.client = OpenRouter(config['model'], exp=exp)
        self.params = generator['params']

    def prompt_path(self, candidate):
        return self.directory/'prompts'/candidate.key/(candidate.id+'.txt')

    def save_prompt(self, candidate, rendered):
        path = self.prompt_path(candidate)
        ledger = self.directory/'runs'/candidate.key/candidate.id
        if (ledger/'request.json').exists() or (ledger/'run.json').exists():
            if not path.is_file() or exact_text(path) != rendered:
                raise RuntimeError(f'Cannot change a prompt after its model call: {candidate.id}')
            self.client.verify(rendered, self.params, ledger)
        elif not path.exists() or exact_text(path) != rendered:
            atomic_text(path, rendered)

    def plan(self):
        rows = read_csv(self.exp/'population/methods.csv')
        sha = read_json(self.exp/'results/anchor.json')['sha']
        names = {row['method'] for row in rows}
        usage = testpilot_materials.snippets(self.adapter.repo, sha, names,
                                             self.params['max_snippets'], self.params['snippet_lines'])
        declarations = testpilot_materials.declarations(self.adapter, self.directory, rows)
        inventory = context.inventory(self.adapter, self.directory, import_hints=True) if rows else None
        limits = self.group['context']['dependency_signatures']
        keys = folder_keys(list(dict.fromkeys(row['class'] for row in rows)))
        hints = {}
        for cls, key in keys.items():
            block, manifest = import_hints.render(inventory, cls, [], limits)
            import_hints.gate(block, manifest, limits)
            hints[cls] = block
            dest = self.directory/'prompts'/key
            atomic_text(dest/'import-hints.txt', block)
            write_json(dest/'import-hints.json', manifest)

        counts, jobs = Counter(), []
        for row, material in zip(rows, declarations, strict=True):
            cls = row['class']
            counts[cls] += 1
            row = {**row, **material, 'snippets': usage[row['method']]}
            available = [p for p, value in zip(OPTIONS, (row['definition'], row['doc'], row['snippets'])) if value]
            stem = f"{cls.rsplit('.', 1)[-1]}_{row['method']}_{counts[cls]}_"
            candidates = []
            for parts in subsets(available):
                for n in range(1, self.params['completions']+1):
                    candidate = Candidate(keys[cls], stem, parts, n, self.params['completions'] > 1)
                    self.save_prompt(candidate, material_prompt(row, parts, hints[cls], candidate.name))
                    candidates.append(candidate)
            jobs.append((row, available, candidates))
        # A changed population or completion count can otherwise leave orphaned
        # candidates under sources/, where the ordinary screen stage would find them.
        plan_path = self.directory/'prompts/plan.json'
        plan = dict(commit=sha, candidates=[[c.key, c.id] for _, _, batch in jobs for c in batch])
        if read_json(plan_path, plan) != plan and (self.directory/'runs').exists():
            raise RuntimeError('TestPilot round plan differs from recorded calls; use --force for a new round')
        write_json(plan_path, plan)
        write_json(self.directory/'prompts/pre-call-check.json',
                   dict(passed=True, checked_at=now(), import_hints_checked=list(keys.values()),
                        generation_commit=sha, material_calls=sum(len(job[2]) for job in jobs)))
        return jobs

    def call(self, candidate, row, parent=None):
        path = self.prompt_path(candidate)
        prompt = exact_text(path)
        ledger = self.directory/'runs'/candidate.key/candidate.id
        calls = []
        for attempt, folder in enumerate((ledger, ledger/'attempt-2'), 1):
            if (folder/'run.json').is_file():
                self.client.verify(prompt, self.params, folder)
                record, response = read_json(folder/'run.json'), exact_text(folder/'response.md')
            else:
                record, response = self.client.complete(prompt, self.params, folder)
            calls.append(record)
            if record['status'] == 'ok':
                break
            if attempt == 1 and not (ledger/'attempt-2/request.json').exists():
                time.sleep(60)
        extraction = extract(response)
        source = extraction.pop('combine_v2')
        file = None
        # Keep malformed Java in an extracted block so javac can explain the failure.
        if source.strip() and extraction['selection'] and not extraction['html_detected']:
            destination = candidate.source(self.directory)
            if destination.exists() and exact_text(destination) != source:
                raise RuntimeError(f'Recorded candidate differs from its response: {destination.name}')
            if not destination.exists():
                atomic_text(destination, source)
            file = str(destination.relative_to(self.exp))
        return dict(id=candidate.id, class_name=candidate.key, target_class=row['class'],
                    target_signature=row['signature'], technique=candidate.label, parent=parent,
                    completion=candidate.completion, prompt=str(path.relative_to(self.exp)),
                    ledger=str(ledger.relative_to(self.exp)), calls=calls, status=calls[-1]['status'],
                    extraction=extraction, file=file, retry=None, outcome='no-test', compiled=False)

    def execute(self, record):
        if not record['file']:
            return
        file = self.exp/record['file']
        slot = validation_id(file, self.directory)
        validation = self.screen.run(file)
        state, error, compiled = outcome(validation)
        bodies = validation['testpilot_bodies']
        record['test_bodies'] = [bodies.get(m['body_hash'], m['body_hash']) for m in validation['methods']]
        record.update(outcome=state, error=error, compiled=compiled,
                      validation=str((self.directory/'validation'/(slot+'.json')).relative_to(self.exp)))

    def refine(self, job):
        row, available, candidates = job
        initial = [self.call(candidate, row) for candidate in candidates]
        for record in initial:
            self.execute(record)
        retries = []
        for candidate, record in zip(candidates, initial, strict=True):
            retry = replace(candidate, retry=True)
            path = self.prompt_path(retry)
            if record['outcome'] in ('pass', 'no-test') and not path.exists():
                continue
            roots = {str(self.exp): '', str(self.exp.resolve()): '',
                     self.screen_adapter.runner.path(self.exp): '',
                     self.screen_adapter.runner.path(self.exp/'work'): 'work/'}
            prompt = exact_text(path) if path.exists() else retry_prompt(
                exact_text(self.exp/record['file']), record['error'], roots)
            self.save_prompt(retry, prompt)
            record['retry'] = retry.id
            retries.append(self.call(retry, row, parent=candidate.id))
        for record in retries:
            self.execute(record)
        records = initial+retries
        first = candidates[0]
        write_json(self.directory/'refinements'/first.key/(first.stem.rstrip('_')+'.json'),
                   dict(method=row, available=available, prompts=records))
        return records

    def generate(self, round_no):
        self.directory = self.exp/'generated'/self.group['name']/f'round{round_no}'
        (self.directory/'validation').mkdir(parents=True, exist_ok=True)
        self.screen = CandidateScreen(self.screen_adapter, self.directory, self.group['name'])
        jobs = self.plan()
        with ThreadPoolExecutor(max_workers=self.params.get('workers', 2)) as pool:
            records = [r for method in pool.map(self.refine, jobs) for r in method]
        write_json(self.directory/'generation.json', records)
        retries = [r for r in records if r['parent']]
        calls, prompts = Counter(), Counter()
        origins = {}
        for r in records:
            if r['parent'] or r['completion'] == 1:
                prompts[r['technique']] += 1
            if r['file']:
                bodies = r['test_bodies']
                if not bodies:
                    # An unparsable class still has an extracted source and a producing prompt.
                    stripped, _ = lexical_masks((self.exp/r['file']).read_text(), text_blocks=True)
                    bodies = [sha256(re.sub(r'\s', '', stripped, flags=re.ASCII).encode()).hexdigest()]
                for body in set(bodies):
                    origins.setdefault(body, []).append(dict(prompt=r['prompt'], file=r['file'], outcome=r['outcome']))
            calls[r['technique']] += len(r['calls'])
        write_json(self.directory/'test-origins.json', origins)
        write_json(self.directory/'refinement-summary.json', dict(
            methods=len(jobs), with_doc=sum('doc' in p for _, p, _ in jobs),
            with_snippets=sum('usage' in p for _, p, _ in jobs),
            prompts_by_label=prompts, calls_by_label=calls, unique_tests=len(origins),
            tests_extracted=sum(bool(r['file']) for r in records), compiled=sum(r['compiled'] for r in records),
            passed=sum(r['outcome']=='pass' for r in records), retries_created=len(retries),
            retries_passed=sum(r['outcome']=='pass' for r in retries),
            no_test=sum(r['outcome']=='no-test' for r in records)))
        return RoundResult(files=[self.exp/r['file'] for r in records if r['file']],
                           calls=[call for r in records for call in r['calls']])
