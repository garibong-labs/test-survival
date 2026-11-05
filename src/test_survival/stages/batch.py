"""Baseline projections of the shared policy survival/coverage worker.

Only measuring sites launch workers. Coverage/reporting consume durable scope
line evidence, including the exact chronological alive-entry population.
"""
from collections import defaultdict
import hashlib
from pathlib import Path

from ..adapters.java_maven import JavaMavenAdapter
from ..adapters.policy_coverage import COVERAGE_VERSION
from ..config import canonical_hash, execution_groups, group_names
from ..io import read_csv, read_json, write_csv, write_json, resolve_path
from ..policy.artifacts import tree_hash
from ..policy.execute import screening_exclusions, settle
from ..rules.survival import FAILURES, dead_method
from ..runner import measurement_session
from ..runner.dispatch import dispatch
from ..work import release_checkout


def identity(t):
    return (t['execution_hash'], t['class'], t['method'])


def catalog(exp, config, frozen):
    """The same content identity as policy snapshots, without copying sources."""
    developers = {g['name'] for g in config['dev']['groups']}
    resources = canonical_hash({name: tree_hash(exp/'dev/src-test-t'/name) for name in ('resources', 'suite')})
    roots = {}; result = {}
    for group, members in frozen.items():
        result[group] = []
        for t in members:
            root = 'dev/src-test-t/java' if group in developers else str(Path(t['file']).parent)
            if root not in roots:
                roots[root] = tree_hash(exp/root, '*.java')
            digest = hashlib.sha256((exp/t['file']).read_bytes()).hexdigest()
            result[group].append(dict(t, file_hash=digest, source_root=root, source_tree_hash=roots[root],
                                      resource_root='dev/src-test-t', resource_tree_hash=resources,
                                      execution_hash=canonical_hash(dict(file=digest, sources=roots[root], resources=resources))))
    return result


class Measurements:
    def __init__(self, exp, config, phase):
        from ..dev_results import imported_for
        self.exp, self.config, self.phase = exp, config, phase
        self.adapter = JavaMavenAdapter(exp, config)
        self.provenance, self.imported = imported_for(exp, config, phase)
        self.frozen = execution_groups(config, read_json(exp/'results/frozen-denominators.json'))
        self.tests = catalog(exp, config, self.frozen)
        self.scope = read_json(exp/'population/scope.json')['classes']
        methods = {g['name']: g['method'] for g in config['generators']}
        self.excluded = screening_exclusions(exp, {}, self.tests, exp,
                                            {g: methods.get(g, 'dev') for g in self.tests}, baseline_prefix='')
        self.runner = self.adapter.provenance()
        self.digest = canonical_hash(dict(version=1, implementation=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), coverage=COVERAGE_VERSION, tests=self.tests,
                                         scope=self.scope, excluded=sorted(self.excluded), runner=self.runner,
                                         inputs={g:self.adapter.run_fingerprint(ts) for g,ts in self.tests.items()}))
        self.unique = {identity(t):t for g,ts in self.tests.items() if g not in self.imported for t in ts}

    def cached(self, index, sha, dead):
        raw = read_json(self.exp/f'results/baseline-batches/{index}.json', {})
        if (raw.get('input_sha256') != self.digest or raw.get('sha') != sha
                or raw.get('coverage_version') != COVERAGE_VERSION or 'universe' not in raw):
            return None
        rows = {identity(t):t for t in raw.get('methods', [])}
        if set(rows) != set(self.unique):
            return None
        if any(rows[k]['state'] == 'dead' and k not in dead for k in rows):
            return None
        if any('lines' not in rows[k] for k in rows if k not in dead):
            return None
        return raw

    def measure(self, index, sha, dead):
        path = self.exp/f'results/baseline-batches/{index}.json'
        prior = read_json(path, {})
        current = prior.get('input_sha256') == self.digest and prior.get('sha') == sha
        old = {identity(t):t for t in prior.get('methods', [])} if current else {}
        kept = {}; requested = []; verdicts = {}
        for k,t in self.unique.items():
            row = old.get(k)
            if k in dead:
                kept[k] = row if row and row.get('state') != 'dead' and 'lines' in row else dead_method(t, dead[k])
            elif row and row.get('state') != 'dead' and 'lines' in row:
                kept[k] = row
            else:
                requested.append(t)
                if row and row.get('state') != 'dead':
                    verdicts[k] = {n:row[n] for n in ('state', 'message', 'flaky') if n in row}
        measured = self.adapter.worker(dict(action='policy-survive', index=index, sha=sha, group='baseline-batch',
                         methods=requested, scope_files=[c['file'] for c in self.scope],
                         scope_classes=[c['class'] for c in self.scope], excluded_files=sorted(self.excluded),
                         scope_universe=True, output=f'results/baseline-batches/{index}-measurement.json',
                         work=f'work/baseline-batches/{index}'))
        for observation in measured['methods']:
            k = identity(observation)
            if k not in self.unique:
                raise ValueError('Unexpected batch method')
            row = {**self.unique[k], **{n:observation[n] for n in ('state','message','lines') if n in observation}, 'flaky':False}
            kept[k] = settle(row, verdicts.get(k), [])
        if set(kept) != set(self.unique):
            raise ValueError('Incomplete baseline batch inventory')
        raw = {**measured, 'execution':'batch', 'input_sha256':self.digest, 'runner_sha256':self.runner,
               'methods':[kept[k] for k in self.unique], 'ignored_settings':['survival.flaky_rerun','survival.environment_errors']}
        raw['coverage_status'] = 'complete' if all('lines' in t for t in raw['methods']) else 'missing'
        write_json(path, raw, compress=True)
        return raw

    def project(self, index, sha, raw):
        from ..dev_results import imported_record
        from .survive import raw_folder
        observed = {identity(t):t for t in raw.get('methods', [])}
        for g, tests in self.tests.items():
            folder = 'control' if self.phase == 'control' else raw_folder(g, self.config, self.provenance)
            path = self.exp/f'results/{folder}/{index}-{g}.json'
            if g in self.imported:
                imported_record(self.exp, str(path.relative_to(self.exp)), sha=sha,
                                method_ids=[t['id'] for t in tests], provenance=self.provenance)
                continue
            rows = [{**observed[identity(t)], **t} for t in tests]
            record = {**raw, 'group':g, 'phase':'control' if self.phase == 'control' else 'A',
                      'methods':rows, 'requested_ids':[t['id'] for t in rows if t['state'] != 'dead'],
                      'input_sha256':self.adapter.run_fingerprint(self.frozen[g]), 'batch_input_sha256':self.digest,
                      'measurement_source':f'results/baseline-batches/{index}.json'}
            if read_json(path) != record:
                write_json(path, record, compress=folder != 'control')


def control(exp, config):
    from ..io import atomic_text
    m = Measurements(exp, config, 'control')
    sha = read_json(exp/'results/anchor.json')['sha']
    with measurement_session(m.adapter, 'control', bool(m.unique)):
        raw = m.cached(0, sha, {}) or m.measure(0, sha, {})
        m.project(0, sha, raw)
    records = {}
    for g in m.tests:
        rows = read_json(exp/f'results/control/0-{g}.json')['methods']
        records[g] = dict(requested=len(rows), passed=sum(t['state']=='pass' for t in rows), failures=[t for t in rows if t['state']!='pass'])
    write_json(exp/'results/control-at-t.json', records)
    atomic_text(exp/'results/control-at-t.md', '# Control at t\n\n'+'\n'.join(f'- {g}: {r["passed"]}/{r["requested"]} passed.' for g,r in records.items())+'\n')
    if any(r['failures'] for r in records.values()):
        raise RuntimeError('Frozen methods failed the control at t')
    project_coverage(exp, config, 0, {g:[t['id'] for t in ts] for g,ts in m.frozen.items()})
    return 'All frozen methods passed at the anchor (batch; scope coverage recorded)'


def survive(exp, config):
    from .survive import aggregate, raw_folder, skipped_commits
    from ..pipeline import log
    m = Measurements(exp, config, 'survive')
    commits = read_csv(exp/'results/commits.csv'); skipped = skipped_commits(exp,config,commits)
    write_csv(exp/'results/skipped-commits.csv', [dict(index=r['index'],sha=r['sha'],reason=skipped[int(r['index'])]) for r in commits if int(r['index']) in skipped], ['index','sha','reason'])
    for r in commits:
        i = int(r['index'])
        if i in skipped:
            for g in m.frozen:
                path = f'results/{raw_folder(g,config,m.provenance)}/{i}-{g}.json'
                if g in m.imported:
                    from ..dev_results import imported_record
                    imported_record(exp,path,sha=r['sha'],provenance=m.provenance)
                else:
                    write_json(exp/path,dict(index=i,sha=r['sha'],group=g,phase='A',status='skipped-untouched'),compress=True)
    evaluated = [r for r in commits if int(r['index']) not in skipped and read_json(exp/f'results/builds/{r["index"]}.json')['build']=='ok']
    known = {}
    def prepare(row, pending):
        i = int(row['index']); dead = {k:n for k,n in known.items() if n < i}
        cached = m.cached(i, row['sha'], dead)
        prior = read_json(exp/f'results/baseline-batches/{i}.json', {})
        if cached is None and pending and any(t.get('state')=='dead' and identity(t) not in dead for t in prior.get('methods', [])):
            return None
        return dead, cached
    def completed(row, raw):
        i = int(row['index']); m.project(i,row['sha'],raw)
        for t in raw['methods']:
            if t['state'] in FAILURES:
                k=identity(t); known[k]=min(i,known.get(k,i))
        log(exp,f'survive: {i} batch recorded',config)
    with measurement_session(m.adapter, 'survive', bool(evaluated)):
        dispatch(evaluated,config['runner']['workers']['survive'],prepare,
                 lambda r,dead:m.measure(int(r['index']),r['sha'],dead),completed)
    curves = aggregate(exp, config)
    entering = read_json(exp/'results/alive-entering.json')
    for row in evaluated:
        i=int(row['index']); project_coverage(exp,config,i,entering[str(i)])
        release_checkout(exp,config,i)
    return 'Final survivors: '+', '.join(f'{g}={curves[-1][g+"_alive"]}' for g in group_names(config))


def project_coverage(exp, config, index, entering):
    """No Java and no XML. Missing hits are unavailable, never measured zero."""
    from ..dev_results import imported_for, imported_record, require_record
    from .survive import raw_folder
    provenance, imported = imported_for(exp,config,'cover')
    adapter = JavaMavenAdapter(exp, config)
    frozen = read_json(exp/'results/frozen-denominators.json')
    for g in group_names(config):
        output = exp/f'results/coverage/{index}-{g}-lines.csv'
        if g in imported:
            imported_record(exp,f'results/raw-coverage/{index}-{g}.json',method_ids=entering[g],provenance=provenance)
            require_record(exp,provenance,str(output.relative_to(exp)))
            continue
        folder = 'control' if index == 0 else raw_folder(g,config,provenance)
        raw = read_json(exp/f'results/{folder}/{index}-{g}.json')
        if raw is None or raw.get('execution') != 'batch':
            raise RuntimeError('Missing batch measurement; resume control/survive')
        wanted = set(entering[g]); rows = [t for t in raw['methods'] if t['id'] in wanted]
        if {t['id'] for t in rows} != wanted or any(t['state']=='dead' for t in rows):
            raise RuntimeError('Batch omitted a method alive entering the index')
        missing = any('lines' not in t or t.get('remeasured_state') not in (None,'pass','fail') for t in rows)
        record = {**raw, 'phase':'B', 'methods':rows, 'requested_ids':sorted(wanted),
                  'input_sha256':adapter.run_fingerprint([t for t in frozen[g] if t['id'] in wanted]),
                  'status':'coverage-missing' if missing else 'evaluated',
                  'coverage_status':'missing' if missing else 'complete'}
        write_json(exp/f'results/raw-coverage/{index}-{g}.json',record,compress=True)
        if missing:
            resolve_path(output).unlink(missing_ok=True)
            output.unlink(missing_ok=True)
            continue
        hits = {tuple(line) for t in rows for line in t['lines']}
        write_csv(output,[dict(file=f,line=n,covered='yes' if (f,n) in hits else 'no') for f,n in raw['universe']],['file','line','covered'],compress=True)


def stage_complete(exp, config, stage):
    """Completed aggregate state must not hide a batch needing coverage repair."""
    if stage not in ('control', 'survive'):
        return True
    from .survive import skipped_commits
    m = Measurements(exp, config, stage)
    commits = read_csv(exp/'results/commits.csv') if stage == 'survive' else []
    skipped = skipped_commits(exp,config,commits) if commits else {}
    rows = commits if stage == 'survive' else [dict(index=0,sha=read_json(exp/'results/anchor.json')['sha'])]
    known = {}
    for r in rows:
        i=int(r['index'])
        if i in skipped or (i and read_json(exp/f'results/builds/{i}.json',{}).get('build') != 'ok'):
            continue
        record = m.cached(i,r['sha'],known)
        if record is None:
            return False
        from .survive import raw_folder
        for g, tests in m.frozen.items():
            if g in m.imported:
                continue
            folder = 'control' if stage == 'control' else raw_folder(g,config,m.provenance)
            projected = read_json(exp/f'results/{folder}/{i}-{g}.json', {})
            if projected.get('batch_input_sha256') != m.digest or {t['id'] for t in projected.get('methods', [])} != {t['id'] for t in tests}:
                return False
        for t in record['methods']:
            if t['state'] in FAILURES:
                known.setdefault(identity(t),i)
    return True
