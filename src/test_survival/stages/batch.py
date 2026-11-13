"""Baseline projections of the shared policy survival/coverage worker.

Only measuring sites launch workers. Coverage/reporting consume durable scope
line evidence, including the exact chronological alive-entry population.
"""
from collections import defaultdict, OrderedDict
import hashlib
from pathlib import Path

from ..denominators import finish_control, frozen_after_control
from ..adapters.java_maven import JavaMavenAdapter
from ..adapters.policy_coverage import COVERAGE_VERSION
from ..config import canonical_hash, derived_source, execution_groups, group_names
from ..io import read_csv, read_json, write_csv, write_json, resolve_path
from ..policy.artifacts import resource_tree_hash, tree_hash
from ..policy.execute import screening_exclusion_details, settle
from ..rules.survival import FAILURES, dead_method
from ..rules.coverage import usable_lines
from ..runner import measurement_session
from ..runner.dispatch import dispatch
from ..work import release_checkout
from ..selection import Selector, affected, carry, executed, counts, FIELDS


def identity(t):
    return (t['execution_hash'], t['class'], t['method'])


def complete_lines(raw, dead):
    return all(usable_lines(t) for t in raw['methods'] if identity(t) not in dead)


def catalog(exp, config, frozen):
    """The same content identity as policy snapshots, without copying sources."""
    developers = {g['name'] for g in config['dev']['groups']}
    resources = resource_tree_hash(exp/'dev/src-test-t')
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
        self.selector = Selector(self.adapter) if affected(config) else None
        self.history = {}
        self.origins = OrderedDict()
        self.provenance, self.imported = imported_for(exp, config, phase)
        self.frozen = execution_groups(config, read_json(exp/'results/frozen-denominators.json') if phase == 'control'
                                       else frozen_after_control(exp, config))
        self.tests = catalog(exp, config, self.frozen)
        self.scope = read_json(exp/'population/scope.json')['classes']
        methods = {g['name']: g['method'] for g in config['generators']}
        self.excluded, self.exclusion_summary = screening_exclusion_details(exp, {}, self.tests, exp,
                                            {g: methods.get(g, 'dev') for g in self.tests}, baseline_prefix='')
        self.runner = self.adapter.provenance()
        inputs = dict(version=1, implementation=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), coverage=COVERAGE_VERSION, tests=self.tests,
                      scope=self.scope, excluded=sorted(self.excluded), runner=self.runner,
                      inputs={g:self.adapter.run_fingerprint(ts) for g,ts in self.tests.items()})
        self.digest = canonical_hash(inputs)
        self.control_digests = {self.digest}
        if phase == 'control':
            # 66ac379 used the whole module as its implementation fingerprint.
            # Admit that exact implementation at the anchor only, while still
            # requiring identical tests, scope, exclusions, runner and inputs.
            self.control_digests.add(canonical_hash(dict(inputs, implementation=
                '38007c6b03cd27c081f193b7af9b266b687c5ac2adfa5ee38522a781bd1c3531')))
        self.derived = {g:source for g in self.tests if g not in self.imported
                        if (source := derived_source(config, g))}
        self.unique = {identity(t):t for g,ts in self.tests.items()
                       if g not in self.imported and g not in self.derived for t in ts}

    def valid_origin(self, row, *, historical=False):
        if row.get('selection_action') != 'carried': return True
        index = row.get('carried_from_index')
        path = resolve_path(self.exp/f'results/baseline-batches/{index}.json')
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False
        signature = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
        cached = self.origins.get(index)
        if cached is None or cached[0] != signature:
            origin = read_json(path, {})
            cached = (signature, origin.get('input_sha256'), {identity(t):t for t in origin.get('methods', [])})
            self.origins[index] = cached
            if len(self.origins) > 32: self.origins.popitem(last=False)
        self.origins.move_to_end(index)
        _, digest, sources = cached
        source = sources.get(identity(row), {})
        return ((historical or digest == self.digest)
                and source.get('selection_action') == 'executed'
                and all(source.get(n) == row.get(n) for n in
                        ('state','lines','dependency_record','last_executed_sha','last_executed_index')))

    def cached(self, index, sha, dead, *, require_lines=True, historical=False):
        raw = read_json(self.exp/f'results/baseline-batches/{index}.json', {})
        if ((not historical and raw.get('input_sha256') not in (self.control_digests if self.phase == 'control' else {self.digest})) or raw.get('sha') != sha
                or raw.get('coverage_version') != COVERAGE_VERSION or 'universe' not in raw):
            return None
        rows = {identity(t):t for t in raw.get('methods', [])}
        if set(rows) != set(self.unique):
            return None
        if any(rows[k]['state'] == 'dead' and k not in dead for k in rows):
            return None
        if any(not self.valid_origin(row, historical=historical) for row in rows.values()):
            return None
        if require_lines and not complete_lines(raw, dead):
            return None
        return raw

    def measure(self, index, sha, dead):
        path = self.exp/f'results/baseline-batches/{index}.json'
        prior = read_json(path, {})
        current = prior.get('input_sha256') == self.digest and prior.get('sha') == sha
        old = {identity(t):t for t in prior.get('methods', [])} if current else {}
        kept = {}; requested = []; verdicts = {}
        context = self.selector.context(index, sha) if self.selector and self.unique else None
        for k,t in self.unique.items():
            row = old.get(k)
            if k in dead:
                kept[k] = row if row and row.get('state') != 'dead' and usable_lines(row) else dead_method(t, dead[k])
            elif row and row.get('state') != 'dead' and usable_lines(row) and self.valid_origin(row):
                kept[k] = row
            elif self.selector and index and not self.selector.decision(self.history.get(k), index, sha, context):
                kept[k] = carry(self.history[k])
            else:
                requested.append(t)
                if row and row.get('state') != 'dead':
                    verdicts[k] = {n:row[n] for n in ('state', 'message', 'flaky') if n in row}
        if not self.unique and self.imported:
            # All roots are imported; local children need projections only.
            # Reuse their parent's universe without starting a Java session.
            from ..dev_results import imported_record
            from .survive import raw_folder
            group = next(iter(self.imported))
            folder = 'control' if self.phase == 'control' else raw_folder(group, self.config, self.provenance)
            source = f'results/{folder}/{index}-{group}.json'
            inherited = imported_record(self.exp, source, sha=sha, provenance=self.provenance)
            measured = dict(index=index, sha=sha, status='evaluated', universe=inherited['universe'],
                            coverage_version=COVERAGE_VERSION, methods=[], files=[], compile=[], runs=[],
                            measurement_source=source)
        else:
            measured = self.adapter.worker(dict(action='policy-survive', index=index, sha=sha, group='baseline-batch',
                         methods=requested, scope_files=[c['file'] for c in self.scope],
                         scope_classes=[c['class'] for c in self.scope], excluded_files=sorted(self.excluded),
                         scope_universe=True, output=f'results/baseline-batches/{index}-measurement.json',
                         work=f'work/baseline-batches/{index}'))
        for observation in measured['methods']:
            k = identity(observation)
            if k not in self.unique:
                raise ValueError('Unexpected batch method')
            row = {**self.unique[k], **{n:observation[n] for n in ('state','message','lines','dependency_record') if n in observation}, 'flaky':False}
            kept[k] = executed(settle(row, verdicts.get(k), []), index, sha, context)
        if set(kept) != set(self.unique):
            raise ValueError('Incomplete baseline batch inventory')
        raw = {**measured, 'execution':'batch', 'input_sha256':self.digest, 'runner_sha256':self.runner,
               'methods':[kept[k] for k in self.unique], 'ignored_settings':['survival.flaky_rerun','survival.environment_errors']}
        raw['selection_counts'] = counts(raw['methods'])
        raw['coverage_status'] = 'complete' if all(usable_lines(t) for t in raw['methods']) else 'missing'
        write_json(path, raw, compress=True)
        return raw

    def project(self, index, sha, raw):
        from ..dev_results import imported_record
        from ..rules.dev_denominator import derive_record
        from .survive import raw_folder
        observed = {identity(t):t for t in raw.get('methods', [])}
        def path_for(group):
            folder = 'control' if self.phase == 'control' else raw_folder(group, self.config, self.provenance)
            return self.exp/f'results/{folder}/{index}-{group}.json'
        for g, tests in self.tests.items():
            path = path_for(g)
            if g in self.imported:
                imported_record(self.exp, str(path.relative_to(self.exp)), sha=sha,
                                method_ids=[t['id'] for t in tests], provenance=self.provenance)
                continue
            if g in self.derived:
                continue
            rows = [{**observed[identity(t)], **t} for t in tests]
            record = {**raw, 'group':g, 'phase':'control' if self.phase == 'control' else 'A',
                      'methods':rows, 'selection_counts':counts(rows), 'requested_ids':[t['id'] for t in rows if t['state'] != 'dead'],
                      'input_sha256':self.adapter.run_fingerprint(self.frozen[g]), 'batch_input_sha256':self.digest,
                      'measurement_source':f'results/baseline-batches/{index}.json'}
            if read_json(path) != record:
                write_json(path, record, compress=self.phase != 'control')
        pending = dict(self.derived)
        while pending:
            ready = [g for g,source in pending.items() if source not in pending]
            if not ready:
                raise RuntimeError('Derived group cycle')
            for g in ready:
                source = pending.pop(g)
                record = derive_record(read_json(path_for(source)), g, self.frozen[g])
                record['selection_counts'] = counts(record['methods'])
                record.update(input_sha256=self.adapter.run_fingerprint(self.frozen[g]),
                              batch_input_sha256=self.digest,
                              measurement_source=str(path_for(source).relative_to(self.exp)),
                              requested_ids=[t['id'] for t in record['methods'] if t['state'] != 'dead'])
                if read_json(path_for(g)) != record:
                    write_json(path_for(g), record, compress=self.phase != 'control')


def control(exp, config):
    m = Measurements(exp, config, 'control')
    write_json(exp/'results/policy/excluded-sources.json', m.exclusion_summary)
    sha = read_json(exp/'results/anchor.json')['sha']
    with measurement_session(m.adapter, 'control', bool(m.unique or not m.imported)):
        raw = m.cached(0, sha, {}, require_lines=config['survival'].get('control_failures') != 'exclude') or m.measure(0, sha, {})
        m.project(0, sha, raw)
    records = {}
    for g in m.tests:
        rows = read_json(exp/f'results/control/0-{g}.json')['methods']
        records[g] = dict(requested=len(rows), passed=sum(t['state']=='pass' for t in rows), failures=[t for t in rows if t['state']!='pass'])
    excluded = finish_control(exp, config, records)
    frozen = frozen_after_control(exp, config)
    project_coverage(exp, config, 0, {g:[t['id'] for t in frozen[g]] for g in m.frozen})
    return (f'Excluded {excluded} frozen methods after control at t (batch)' if excluded else
            'All frozen methods passed at the anchor (batch; scope coverage recorded)')


def survive(exp, config):
    from .survive import aggregate, raw_folder, skipped_commits
    from ..pipeline import log
    m = Measurements(exp, config, 'survive')
    write_json(exp/'results/policy/excluded-sources.json', m.exclusion_summary)
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
    if m.selector:
        anchor = read_json(exp/'results/anchor.json')['sha']
        control = m.cached(0, anchor, {})
        if control:
            m.history.update({identity(t):t for t in control['methods'] if t['state'] == 'pass'})
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
        if m.selector:
            m.history.update({identity(t):t for t in raw['methods'] if t['state'] == 'pass'})
        log(exp,f'survive: {i} batch recorded',config)
    with measurement_session(m.adapter, 'survive', bool(evaluated and (m.unique or not m.imported))):
        dispatch(evaluated,1 if m.selector else config['runner']['workers']['survive'],prepare,
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
    frozen = frozen_after_control(exp, config)
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
        missing = any(not usable_lines(t) for t in rows)
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


def completed_items(exp, config, stage, *, historical=False):
    """Read-only completeness audit shared by resume, log and panel progress."""
    from .survive import skipped_commits, raw_folder
    # Status visits every stage, including those whose inputs have not been
    # produced yet. Missing inputs are pending, not an empty completed batch.
    frozen = (read_json(exp/'results/frozen-denominators.json') if stage == 'control'
              else frozen_after_control(exp, config))
    scope = read_json(exp/'population/scope.json')
    anchor = read_json(exp/'results/anchor.json')
    if (not isinstance(frozen, dict) or not isinstance(scope, dict)
            or not isinstance(scope.get('classes'), list)
            or not isinstance(anchor, dict) or not anchor.get('sha')):
        return [], None
    commits = []
    if stage == 'survive':
        path = exp/'results/commits.csv'
        changes = read_json(exp/'results/commit-paths.json')
        if not path.exists() or not isinstance(changes, dict):
            return [], None
        commits = read_csv(path)
        if any(r['index'] not in changes for r in commits):
            return [], None
    m = Measurements(exp, config, stage)
    skipped = skipped_commits(exp,config,commits) if commits else {}
    rows = commits if stage == 'survive' else [dict(index=0,sha=anchor['sha'])]
    known = {}; completed = []
    for r in rows:
        i=int(r['index'])
        paths = [exp/f'results/{"control" if stage == "control" else raw_folder(g,config,m.provenance)}/{i}-{g}.json' for g in m.frozen]
        if i in skipped:
            if all((raw := read_json(p, {})).get('status') == 'skipped-untouched' and raw.get('sha') == r['sha'] for p in paths):
                completed.append(paths)
            continue
        build = read_json(exp/f'results/builds/{i}.json',{})
        if i and build.get('build') != 'ok':
            if build.get('build') == 'fail' and build.get('sha') == r['sha']:
                completed.append([])
            continue
        # Coverage repair preserves recorded survival verdicts. They still
        # justify later dead omissions while this index remains incomplete.
        record = m.cached(i,r['sha'],known,require_lines=False,historical=historical)
        if record is None:
            continue
        valid = (stage == 'control' and config['survival'].get('control_failures') == 'exclude') or complete_lines(record, known)
        for g, tests in m.frozen.items():
            if g in m.imported:
                continue
            folder = 'control' if stage == 'control' else raw_folder(g,config,m.provenance)
            projected = read_json(exp/f'results/{folder}/{i}-{g}.json', {})
            if (not historical and projected.get('batch_input_sha256') != m.digest) or {t['id'] for t in projected.get('methods', [])} != {t['id'] for t in tests}:
                valid = False
        if valid:
            completed.append([exp/f'results/baseline-batches/{i}.json', *paths])
        for t in record['methods']:
            if t['state'] in FAILURES:
                known.setdefault(identity(t),i)
    return completed, len(rows)


def stage_complete(exp, config, stage):
    """Completed aggregate state must not hide a batch needing coverage repair."""
    if stage not in ('control', 'survive'):
        return True
    completed, total = completed_items(exp, config, stage)
    return total is not None and len(completed) == total
