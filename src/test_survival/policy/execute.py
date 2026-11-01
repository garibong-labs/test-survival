"""Commit execution deduplicated across policies by source hash and index."""
from collections import defaultdict
from pathlib import Path
import threading

from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json, write_json, write_csv
from ..rules.survival import FAILURES, dead_method
from ..runner.dispatch import dispatch
from .artifacts import snapshot
from .config import COVERAGE_VERSION, MARKERS, active_plan, reusable_run, unmet_rows
from .segments import execution_id, key, identity, segment, regenerate, assemble, evaluate


class Runs:
    """Per-index execution of every method the policies need, deduplicated by
    source digest. Records are written by the measuring sites (prefetch windows
    and regeneration batches); `get` only reads them."""

    def __init__(self, baseline, step):
        self.b = baseline
        self.step = step
        self.adapter = JavaMavenAdapter(baseline.exp, baseline.execution_config)
        self.ready = False
        self.lock = threading.Lock()
        self.needed = set()
        self.catalog = defaultdict(dict)
        self.excluded = set()
        self.missing = {}

    def initialize(self):
        with self.lock:
            if self.ready:
                return
            write_json(self.b.exp/'results/anchor.json', self.b.anchor)
            if self.adapter.build(0, self.b.anchor['sha'])['build'] != 'ok':
                raise RuntimeError('Policy anchor build failed')
            self.adapter.setup_runner()
            self.ready = True

    def register(self, tests):
        for t in tests:
            self.catalog[execution_id(t)][identity(t)] = t

    def path(self, index, digest):
        return self.b.exp/f'results/policy/runs/{digest}/{index}.json'

    def required(self, digest, alive):
        """Members needed at an index: the alive entrants of the executing policies.

        Baseline raw records and pool screening carry pass/fail only, so a method
        a fill policy carries over, and a pool at its own regeneration commit, are
        executed like any other member: a policy table needs every surviving
        method's executed lines at every commit (changed-line coverage).
        """
        return {ident for ident, t in self.catalog[digest].items() if alive is None or key(t) in alive}

    def run_index(self, index, digests, *, alive=None, dead_at=None, label=None, evaluated=None):
        """Measure the required members of `digests` at `index` that have no usable
        row yet, in one worker batch; the other rows of each record are kept
        when the record itself is current.

        A usable row is a real observation (not a dead or not-selected marker)
        with its executed lines. `evaluated` names the members whose prior rows
        the policies evaluated from (None: every required member, the window's
        case): their lineless rows keep their verdict when measured again, and
        at the regeneration batch they are not measured at all, since that batch
        only adds the new composition. `label` names that second batch so its
        evidence does not overwrite the window's."""
        digests = sorted(digests)
        required = {h: self.required(h, alive) for h in digests}
        consumed = {h: (required[h] if evaluated is None else {i for i, t in self.catalog[h].items() if key(t) in evaluated}) for h in digests}
        kept = {h: {} for h in digests}
        verdicts = {h: {} for h in digests}
        reasons = {}
        collected = []
        for h in digests:
            prior = read_json(self.path(index,h), {})
            state = read_json(self.b.exp/f'state/policy-run-{h}-{index}.json', {})
            if state.get('status') == 'completed' and reusable_run(prior, required_methods=required[h]):
                continue
            current = state.get('status') == 'completed' and reusable_run(prior)  # an old-format record is replaced whole
            rows = {identity(t): t for t in prior.get('methods', []) if 'class' in t and 'method' in t} if current else {}
            for ident, t in self.catalog[h].items():
                row = rows.get(ident)
                usable = row is not None and row.get('state') not in MARKERS and 'lines' in row
                if ident not in required[h]:
                    # Not needed here: keep a usable row another batch measured
                    # (the regeneration batch's new entrants), else write a marker.
                    if usable:
                        kept[h][ident] = row
                    elif key(t) in (dead_at or {}):
                        kept[h][ident] = dead_method(t, dead_at[key(t)])
                    else:
                        # A fill policy can select only part of a pool/file.
                        # Keep its full inventory without executing unused tests.
                        kept[h][ident] = dict(t, state='not-selected', message='not executed; outside active segments', flaky=False, lines=[])
                elif usable or (evaluated is not None and ident in consumed[h] and row is not None and row.get('state') not in MARKERS):
                    kept[h][ident] = row
                else:
                    if row is not None and row.get('state') not in MARKERS and ident in consumed[h]:
                        # The per-file fallback kept this survival verdict when the
                        # coverage JVM died, and the policies evaluated from it.
                        # Measuring again refreshes the lines, never the verdict.
                        verdicts[h][ident] = {k: row[k] for k in ('state', 'message', 'flaky') if k in row}
                    collected.append(t)
            if any('lines' not in row for row in kept[h].values()):
                reasons[h] = prior.get('coverage_reason', '')
        batch = None
        def measure():
            nonlocal batch
            if batch is not None:
                return batch
            self.initialize()
            sha = self.b.by_index[index]['sha']
            if self.adapter.build(index, sha)['build'] != 'ok':
                raise RuntimeError('Build differs from baseline at evaluable index '+str(index))
            suffix = f'-{label}' if label else ''
            batch = self.adapter.worker(dict(action='policy-survive', index=index, sha=sha, group='policy-survival',
                                            methods=collected, scope_files=[c['file'] for c in self.b.scope['classes']],
                                            scope_classes=[c['class'] for c in self.b.scope['classes']],
                                            excluded_files=sorted(self.excluded_for(collected)),
                                            output=f'results/policy/batches/{index}{suffix}.json', work=f'work/policy-survive/{index}{suffix}'))
            return batch
        for digest in digests:
            self.needed.add((digest,index))
            methods = list(self.catalog[digest].values())
            path = self.path(index,digest)
            def execute():
                unsettled = []
                disagreed = []
                observations = dict(kept[digest])
                missing = set(self.catalog[digest]) - observations.keys()
                raw = measure() if missing else {}
                for t in raw.get('methods', []):
                    if execution_id(t) != digest or identity(t) not in missing:
                        continue
                    observations[identity(t)] = settle(t, verdicts[digest].get(identity(t)), unsettled, disagreed)
                if set(observations) < set(self.catalog[digest]):
                    raise ValueError('Incomplete policy batch inventory')
                observed = [observations[identity(t)] for t in methods]
                scope = {c['file'] for c in self.b.scope['classes']}
                observed = [{**t, 'message':t.get('message', '')[:4000], **({'lines':[line for line in t['lines'] if line[0] in scope]} if 'lines' in t else {})}
                            for t in observed]
                complete = all('lines' in t for t in observed)
                reason = (raw.get('coverage_reason') or reasons.get(digest, '')
                          or (f'remeasure did not execute {len(unsettled)} passing method(s): '+', '.join(sorted(set(unsettled))) if unsettled else ''))
                if complete and disagreed:
                    reason = f'remeasure disagreed with the recorded verdict for {len(disagreed)} method(s)'
                write_json(path, dict(index=index, sha=self.b.by_index[index]['sha'], status='evaluated',
                                      coverage_version=COVERAGE_VERSION,
                                      coverage_status='complete' if complete else 'missing',
                                      coverage_reason=reason if (not complete or disagreed) else '',
                                      methods=observed))
            self.step(f'run-{digest}-{index}', execute, outputs=[path], required_methods=required[digest])
            self.track_missing(index, digest, read_json(path))

    def excluded_for(self, methods):
        """The exclusions that lie under the source roots of a batch (the worker's
        root rule: `source_root`, else the file's folder; compared as resolved paths)."""
        exp = self.b.exp.resolve()
        roots = {(exp/source_root_of(t)).resolve() for t in methods}
        return {p for p in self.excluded if any((exp/p).resolve().is_relative_to(r) for r in roots)}

    def track_missing(self, index, digest, saved):
        """Keep `missing` in step with the record on disk: a lineless observation
        is listed with the record's reason, and a later measurement that gives it
        lines clears it. Marker rows are not measurements in either direction."""
        reason = saved.get('coverage_reason') or 'missing per-test coverage'
        for t in saved.get('methods', []):
            ref = self.catalog[digest].get(identity(t))
            if ref is None or t.get('state') in MARKERS:
                continue
            k = (index, key(ref))
            if 'lines' in t:
                self.missing.pop(k, None)
            else:
                self.missing[k] = reason

    def prefetch(self, indices, digests, *, alive, dead_at):
        """Dispatch a fixed-composition window using completed observations.

        A regeneration boundary ends the window: fill/reset needs exact prior
        outcomes before its next composition (and hence live union) is known.
        """
        if not digests:
            return
        known = {k:v for k,v in dead_at.items() if k not in alive}
        def job(index, context):
            self.run_index(index, digests, **context)
            return [read_json(self.path(index,h), {}) for h in sorted(digests)]
        def prepare(index, pending):
            context = dict(alive=alive-known.keys(), dead_at=dict(known))
            cached = True
            for h in digests:
                prior = read_json(self.path(index,h), {})
                state = read_json(self.b.exp/f'state/policy-run-{h}-{index}.json', {})
                if state.get('status') != 'completed' or not reusable_run(prior):
                    cached = False
                    continue
                blocking = unmet_rows(prior, self.required(h, context['alive']))
                if blocking is None or blocking:
                    # A dead or not-selected marker may be excused by a death an
                    # earlier pending index has yet to report, so wait for it; rows
                    # that merely lack their lines are measured without waiting,
                    # which keeps the window parallel after a run of JVM failures.
                    if pending and blocking and any(t.get('state') in MARKERS for t in blocking):
                        return None  # Earlier results may validate these omissions.
                    cached = False
            return context,job(index,context) if cached else None
        def completed(index, records):
            # Rows may lack file_hash (records written before this layout);
            # resolve each row to its catalog test by identity for the key.
            for h, record in zip(sorted(digests), records):
                tests = self.catalog[h]
                for t in record.get('methods', []):
                    ref = tests.get(identity(t))
                    if ref is None:
                        continue
                    k = key(ref)
                    if k in alive and t['state'] in FAILURES:
                        known[k] = min(index,known.get(k,index))
        dispatch(indices,self.b.execution_config['runner']['workers']['survive'],prepare,job,completed)

    def get(self, index, digest):
        """Read the record a measuring site wrote; never execute."""
        result = read_json(self.path(index,digest), None)
        if result is None:
            raise ValueError(f'Missing policy run record at index {index} for {digest}')
        if set(self.catalog[digest]) != {identity(t) for t in result['methods']}:
            raise ValueError('Shared run method inventory changed; use --force')
        return result

    def carry(self, index, digest):
        self.needed.add((digest,index))
        path = self.path(index,digest)
        status = 'skipped-untouched' if index in self.b.skipped else 'build-fail'
        self.step(f'run-{digest}-{index}', lambda:write_json(path, dict(index=index, sha=self.b.by_index[index]['sha'],
                  status=status, coverage_version=COVERAGE_VERSION, coverage_status='not-evaluated', methods=[])), outputs=[path])


def source_root_of(test):
    """The worker's root rule (policy_coverage.run, snapshot_compile.filtered_roots)."""
    return test.get('source_root', str(Path(test['file']).parent))


def settle(row, verdict, unsettled, disagreed):
    """Merge a remeasured row with the verdict the policies evaluated from.

    - no verdict: the row as measured;
    - same state, or a non-pass verdict (the member is dead here and its lines
      are never read): the measured row with the verdict's message, settled;
    - pass verdict, run again with another outcome: keep the verdict and take
      the lines, recording the disagreement;
    - pass verdict, not executed this time (compile failure, absent): the row
      stays lineless so the index is listed and measured again.
    """
    if verdict is None:
        return row
    state = row.get('state')
    if state == verdict['state'] or verdict['state'] != 'pass':
        return {**row, **verdict, 'remeasured_state': state}
    if state in ('pass', 'fail'):
        disagreed.append(state)
        return {**row, **verdict, 'remeasured_state': state}
    unsettled.append(str(state))
    return {**{k: v for k, v in row.items() if k != 'lines'}, **verdict, 'remeasured_state': state}


def generator_methods(b):
    """Group name -> generation method; developer groups are 'dev'."""
    generators = {g['name']:g['method'] for g in b.config['generators']}
    return {g: generators.get(g, 'dev') for g in b.groups}


def baseline_tests(b):
    methods = generator_methods(b)
    return {g: snapshot(b.path, b.exp, b.frozen[g], group=g, method=methods[g],
                        scope=b.scope['classes'], prefix='results/policy/baseline-sources') for g in b.groups}


def survive(b, plan, pools, step):
    plan = active_plan(b, plan)
    pools = {k:v for k,v in pools.items() if any(p['group'] == k[0] and p['index'] == k[1] for p in plan['pools'])}
    frozen = baseline_tests(b)
    runs = Runs(b, step)
    runs.excluded = screening_exclusions(b.exp, pools, frozen, b.path, generator_methods(b))
    for tests in [*frozen.values(), *pools.values()]:
        runs.register(tests)
    policies = {p['name']:p for p in b.options['set']}
    current = {p:{g:segment(1,0,tests,[]) for g,tests in frozen.items()} for p in policies}
    segments = {p:{g:[s] for g,s in gs.items()} for p,gs in current.items()}
    alive = {p:{g:{key(t) for t in s['tests']} for g,s in gs.items()} for p,gs in current.items()}
    points = {p:{(x['group'],x['index']) for x in plan['points'] if x['trigger']==policy['trigger'] and x['index'] is not None} for p,policy in policies.items()}
    curves = {p:[] for p in policies}; failures = {p:[] for p in policies}; alive_at = {p:{} for p in policies}
    entering_at = {p:{} for p in policies}
    def row_for(commit, p, event, stats):
        row = {k:commit[k] for k in ('index','sha','date','subject','evaluated')}
        row['event'] = event
        for g in b.groups:
            row.update({g+'_'+k:v for k,v in stats[g].items()})
        curves[p].append(row)
    dead_at = {}
    boundaries = sorted({i for ps in points.values() for _,i in ps})
    prefetched_until = -1
    def entering():
        # Members policy run records are read for: segment 1 is evaluated from
        # baseline raw records and never reads them.
        return {ident for p in policies for g,s in current[p].items() if s['segment'] > 1 for ident in alive[p][g]}
    for commit in b.curves:
        index = int(commit['index']); evaluable = index > 0 and b.evaluable(index)
        if index > prefetched_until:
            end = next((i for i in boundaries if i >= index),int(b.curves[-1]['index']))
            window_digests = {execution_id(t) for gs in current.values() for s in gs.values() if s['segment'] > 1 for t in s['tests']}
            runs.prefetch([int(c['index']) for c in b.curves if index <= int(c['index']) <= end and b.evaluable(int(c['index']))],
                          window_digests,alive=entering(),dead_at=dead_at)
            prefetched_until = end
        before_evaluation = entering()
        regenerated = set()
        for p, policy in policies.items():
            entering_at[p][index] = {g:set(ids) for g,ids in alive[p].items()}
            stats = {}
            for g in b.groups:
                s = current[p][g]; observations = []
                if evaluable:
                    if s['segment'] == 1:
                        raw = b.raw(index,g)
                        by_id = {r['id']:r for r in raw['methods']}
                        observations = [{**by_id[t['origin_id']], 'id':key(t)} for t in s['tests']]
                    else:
                        observations = assemble(s['tests'], {h:runs.get(index,h) for h in sorted({execution_id(t) for t in s['tests']})})
                if index and not evaluable and s['segment'] > 1:
                    for digest in {execution_id(t) for t in s['tests']}:
                        runs.carry(index,digest)
                alive[p][g], lost, stats[g] = evaluate(s, alive[p][g], observations, evaluated=evaluable)
                dead_at.update({t['id']:index for t in lost})
                failures[p].extend(dict(index=index,sha=commit['sha'],group=g,segment=s['segment'],id=t['id'],kind=t['state'],flaky=t.get('flaky',False)) for t in lost)
            row_for(commit,p,'evaluate',stats)
            triggered = [g for g in b.groups if (g,index) in points[p]]
            if triggered:
                for g in triggered:
                    s = regenerate(current[p][g],alive[p][g],pools[(g,index)],action=policy['action'],developer=g in b.dev,seed=b.options['fill_seed'],index=index)
                    current[p][g] = s; segments[p][g].append(s); alive[p][g] = {key(t) for t in s['tests']}
                    entering_at[p][index][g].update(alive[p][g])
                    regenerated.update(execution_id(t) for t in s['tests'])
                    _, _, stats[g] = evaluate(s,alive[p][g],[],evaluated=False)
                # Non-triggered groups do not lose tests a second time on the regeneration row.
                for g in b.groups:
                    stats[g]['lost'] = 0
                row_for(commit,p,'regenerate',stats)
            alive_at[p][str(index)] = {g:[t for t in current[p][g]['tests'] if key(t) in alive[p][g]] for g in b.groups}
        if regenerated:
            # Every policy has evaluated this commit, so the new compositions are
            # executed once, in one batch across policies and sources, for the
            # coverage of the regeneration commit. Members that entered this
            # commit stay required and keep the rows the window measured here,
            # even without lines (the policies have evaluated this commit from
            # them); only members without a usable row are executed.
            runs.run_index(index,sorted(regenerated),alive=before_evaluation|entering(),dead_at=dead_at,label='regenerate',evaluated=before_evaluation)
            for digest in sorted(regenerated):
                runs.get(index,digest)  # inventory check of the record just written
    baseline_alive = {g:{key(t) for t in tests} for g,tests in frozen.items()}
    baseline_at = {}
    for commit in b.curves:
        index = int(commit['index'])
        if index and b.evaluable(index):
            for g,tests in frozen.items():
                raw = {r['id']:r for r in b.raw(index,g)['methods']}
                baseline_alive[g] -= {key(t) for t in tests if raw[t['origin_id']]['state'] != 'pass'}
        baseline_at[str(index)] = {g:[t for t in tests if key(t) in baseline_alive[g]] for g,tests in frozen.items()}
    write_json(b.exp/'results/policy/none/alive.json', baseline_at)
    for p in policies:
        directory = b.exp/'results/policy'/p
        write_json(directory/'segments.json', segments[p])
        write_json(directory/'alive.json', alive_at[p])
        write_csv(directory/'survival.csv',curves[p],list(curves[p][0]))
        write_csv(directory/'first-failures.csv',failures[p],['index','sha','group','segment','id','kind','flaky'])
        missing = []
        for index in sorted({i for i,h in runs.missing}):
            for g, gs in segments[p].items():
                active = [s for s in gs if s['start'] <= index][-1]
                reasons = {runs.missing[index,key(t)] for t in active['tests']
                           if key(t) in entering_at[p][index][g] and (index,key(t)) in runs.missing}
                if reasons:
                    missing.append(dict(index=index,sha=b.by_index[index]['sha'],group=g,reason='; '.join(sorted(reasons))))
        write_csv(directory/'coverage-missing.csv',missing,['index','sha','group','reason'])
    write_json(b.exp/'results/policy/needed-runs.json',[dict(execution_hash=h,index=i) for h,i in sorted(runs.needed)])


# Subdirectories a generator writes beside its kept tests that are never
# helper sources. KTester's repair loop keeps every attempt under
# `<test_classes>/temp/` (KTester code/procedure/post_process.py), hundreds of
# files per pool, some of them unparsable.
GENERATOR_SCRATCH_DIRS = ('temp',)


def screening_exclusions(exp, pools, frozen=None, baseline=None, methods=None):
    """Exclude rejected test candidates and generator scratch files from policy jobs.

    `pools` maps (group, index) to the pool's tests; `frozen` maps a group to its
    frozen snapshot under `results/policy/baseline-sources`, whose screening
    records live in the `baseline` experiment; `methods` maps a group to its
    generation method and is required with `frozen`. Developer groups (`dev`)
    get no scratch scan in either tree: a developer test tree may have a `temp`
    package of its own. Successfully compiled helpers and other unscreened
    siblings stay available to javac and the helper inventory, as before.
    Scratch subtrees are excluded because a snapshot copies every sibling of a
    kept test: without this the whole-root compile fails at every index and an
    unparsable attempt can abort the per-file fallback. Pool artifacts on disk
    and their stored hashes are not modified; a per-tree summary goes to
    results/policy/excluded-sources.json.
    """
    if (pools or frozen) and methods is None:
        raise ValueError('screening_exclusions: methods (group -> generation method) is required')
    methods = methods or {}
    for group in {g for g, _ in pools} | set(frozen or {}):
        if group not in methods:
            raise ValueError(f'screening_exclusions: no generation method for group {group}')
    excluded = set(); summary = {}
    def collect(label, group, records_root, snapshot_root, tests):
        kept = {(exp/t['file']).resolve() for t in tests}
        rejected = set()
        for path in (records_root/'generated'/group).glob('round*/validation/*.json'):
            record = read_json(path)
            candidate = record.get('file')
            helper = not record.get('methods') and (record.get('compile') or {}).get('exit_code') == 0
            if candidate and not helper and (snapshot_root/candidate).resolve() not in kept:
                rejected.add((snapshot_root/candidate).resolve())
        scratch = set()
        if methods.get(group) != 'dev':
            for source_root in {(exp/t['source_root']).resolve() for t in tests if t.get('source_root')}:
                for name in GENERATOR_SCRATCH_DIRS:
                    scratch.update(p.resolve() for p in (source_root/name).rglob('*.java'))
            scratch -= kept
        for p in rejected | scratch:
            excluded.add(str(p.relative_to(exp.resolve())))
        summary[label] = dict(kept=len(kept), rejected=len(rejected), scratch=len(scratch))
    for (group, index), tests in pools.items():
        root = exp/f'results/policy/pools/{group}/{index}'
        collect(f'{group}/{index}', group, root, root, tests)
    for group, tests in (frozen or {}).items():
        if tests and baseline is not None and methods.get(group) != 'dev':
            collect(f'{group}/t', group, Path(baseline), exp/'results/policy/baseline-sources', tests)
    write_json(exp/'results/policy/excluded-sources.json', summary)
    return excluded
