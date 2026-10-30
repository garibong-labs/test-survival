"""Commit execution deduplicated across policies by source hash and index."""
from collections import defaultdict
import threading

from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json, write_json, write_csv
from ..rules.survival import FAILURES, dead_method
from ..runner.dispatch import dispatch
from .artifacts import snapshot
from .config import COVERAGE_VERSION, active_plan, reusable_run
from .segments import execution_id, key, identity, segment, regenerate, assemble, evaluate


class Runs:
    def __init__(self, baseline, step):
        self.b = baseline
        self.step = step
        self.adapter = JavaMavenAdapter(baseline.exp, baseline.execution_config)
        self.ready = False
        self.lock = threading.Lock()
        self.needed = set()
        self.catalog = defaultdict(dict)
        self.baseline_catalog = {}
        self.pool_catalog = defaultdict(dict)
        self.excluded = set()
        self.missing = {}
        self.entry_context = {}

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

    def imports(self, index, digest):
        imported = {}
        for group, baseline_tests in self.baseline_catalog.items():
            candidates = [t for t in baseline_tests if execution_id(t) == digest]
            if candidates:
                raw = {r['id']:r for r in self.b.raw(index,group)['methods']}
                # Baseline survival may have stopped running this method even
                # though a later policy pool revives the same physical source.
                imported.update({identity(t):{**t, **raw[t['origin_id']],
                                               'file_hash':t['file_hash'], 'execution_hash':execution_id(t)} for t in candidates
                                 if raw[t['origin_id']]['state'] not in ('dead', 'not-selected')})
        imported.update(self.pool_catalog.get((index,digest), {}))
        return imported

    def run_index(self, index, digests, *, alive=None, dead_at=None):
        if alive is not None:
            self.entry_context[index] = dict(alive=set(alive), dead_at=dict(dead_at or {}))
        digests = sorted(digests)
        imported = {h:self.imports(index,h) for h in digests}
        inactive = {}
        if alive is not None:
            for h in digests:
                inactive[h] = {}
                for ident, t in self.catalog[h].items():
                    if key(t) in alive or ident in imported[h]:
                        continue
                    if key(t) in (dead_at or {}):
                        inactive[h][ident] = dead_method(t, dead_at[key(t)])
                    else:
                        # A fill policy can select only part of a pool/file.
                        # Keep its full inventory without executing unused tests.
                        inactive[h][ident] = dict(t, state='not-selected', message='not executed; outside active segments', flaky=False, lines=[])
        # Pools can introduce new live demand later at this same index. Other
        # available imports do not make an inactive method required.
        required = {h:{ident for ident,t in self.catalog[h].items() if alive is None or key(t) in alive}
                    | self.pool_catalog.get((index,h), {}).keys() for h in digests}
        collected = []
        for h in digests:
            prior = read_json(self.path(index,h), {})
            state = read_json(self.b.exp/f'state/policy-run-{h}-{index}.json', {})
            if state.get('status') == 'completed' and reusable_run(prior, required_methods=required[h]):
                continue
            collected.extend(t for ident,t in self.catalog[h].items() if ident not in imported[h] and ident not in inactive.get(h, {}))
        batch = None
        def measure():
            nonlocal batch
            if batch is not None:
                return batch
            self.initialize()
            sha = self.b.by_index[index]['sha']
            if self.adapter.build(index, sha)['build'] != 'ok':
                raise RuntimeError('Build differs from baseline at evaluable index '+str(index))
            batch = self.adapter.worker(dict(action='policy-survive', index=index, sha=sha, group='policy-survival',
                                            methods=collected, scope_files=[c['file'] for c in self.b.scope['classes']],
                                            scope_classes=[c['class'] for c in self.b.scope['classes']],
                                            excluded_files=sorted(self.excluded),
                                            output=f'results/policy/batches/{index}.json', work=f'work/policy-survive/{index}'))
            return batch
        for digest in digests:
            self.needed.add((digest,index))
            methods = list(self.catalog[digest].values())
            path = self.path(index,digest)
            def execute():
                observations = {**imported[digest], **inactive.get(digest, {})}
                missing = set(self.catalog[digest]) - observations.keys()
                raw = measure() if missing else {}
                observations.update({identity(t):t for t in raw.get('methods', []) if execution_id(t) == digest and identity(t) in missing})
                if set(observations) < set(self.catalog[digest]):
                    raise ValueError('Incomplete policy batch inventory')
                observed = [observations[identity(t)] for t in methods]
                scope = {c['file'] for c in self.b.scope['classes']}
                observed = [{**t, 'message':t.get('message', '')[:4000], **({'lines':[line for line in t['lines'] if line[0] in scope]} if 'lines' in t else {})}
                            for t in observed]
                complete = all('lines' in t for t in observed)
                write_json(path, dict(index=index, sha=self.b.by_index[index]['sha'], status='evaluated',
                                      imported=not missing and not inactive.get(digest), coverage_version=COVERAGE_VERSION,
                                      coverage_status='complete' if complete else 'missing',
                                      coverage_reason='' if complete else raw.get('coverage_reason', 'imported evidence has no per-test coverage'),
                                      methods=observed))
            self.step(f'run-{digest}-{index}', execute, outputs=[path], required_methods=required[digest])
            saved = read_json(path)
            if saved.get('coverage_reason') == 'batch JVM failed or timed out':
                for t in saved['methods']:
                    if 'lines' not in t:
                        self.missing[index, key(t)] = saved['coverage_reason']

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
                required = {ident for ident,t in self.catalog[h].items() if key(t) in context['alive']}
                required |= self.pool_catalog.get((index,h), {}).keys()
                if not reusable_run(prior, required_methods=required):
                    if pending:
                        return None  # Earlier results may validate these omissions.
                    cached = False
            return context,job(index,context) if cached else None
        def completed(index, records):
            # Record rows imported from the baseline (and dead markers) carry no
            # file_hash; resolve each row to its catalog test by identity for the key.
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
        # Re-reading a measured batch must retain its entry demand; otherwise
        # legitimate inactive markers would look like missing observations.
        self.run_index(index, [digest], **self.entry_context.get(index, {}))
        result = read_json(self.path(index,digest))
        if set(self.catalog[digest]) != {identity(t) for t in result['methods']}:
            raise ValueError('Shared run method inventory changed; use --force')
        return result

    def carry(self, index, digest):
        self.needed.add((digest,index))
        path = self.path(index,digest)
        status = 'skipped-untouched' if index in self.b.skipped else 'build-fail'
        self.step(f'run-{digest}-{index}', lambda:write_json(path, dict(index=index, sha=self.b.by_index[index]['sha'],
                  status=status, coverage_version=COVERAGE_VERSION, coverage_status='not-evaluated', methods=[])), outputs=[path])


def baseline_tests(b):
    groups = {}
    generators = {g['name']:g['method'] for g in b.config['generators']}
    for g in b.groups:
        groups[g] = snapshot(b.path, b.exp, b.frozen[g], group=g, method=generators.get(g,'dev'),
                             scope=b.scope['classes'], prefix='results/policy/baseline-sources')
    return groups


def survive(b, plan, pools, step):
    plan = active_plan(b, plan)
    pools = {k:v for k,v in pools.items() if any(p['group'] == k[0] and p['index'] == k[1] for p in plan['pools'])}
    frozen = baseline_tests(b)
    runs = Runs(b, step)
    runs.baseline_catalog = frozen
    runs.excluded = screening_exclusions(b.exp, pools)

    for (_,index), tests in pools.items():
        for t in tests:
            runs.pool_catalog[index,execution_id(t)][identity(t)] = dict(t,state='pass',flaky=False)
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
    for commit in b.curves:
        index = int(commit['index']); evaluable = index > 0 and b.evaluable(index)
        if index > prefetched_until:
            end = next((i for i in boundaries if i >= index),int(b.curves[-1]['index']))
            digests = {execution_id(t) for gs in current.values() for s in gs.values() if s['segment'] > 1 for t in s['tests']}
            entering = {ident for groups in alive.values() for ids in groups.values() for ident in ids}
            runs.prefetch([int(c['index']) for c in b.curves if index <= int(c['index']) <= end and b.evaluable(int(c['index']))],
                          digests,alive=entering,dead_at=dead_at)
            prefetched_until = end
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
                    for digest in sorted({execution_id(t) for t in s['tests']}):
                        runs.get(index,digest)
                    current[p][g] = s; segments[p][g].append(s); alive[p][g] = {key(t) for t in s['tests']}
                    entering_at[p][index][g].update(alive[p][g])
                    _, _, stats[g] = evaluate(s,alive[p][g],[],evaluated=False)
                # Non-triggered groups do not lose tests a second time on the regeneration row.
                for g in b.groups:
                    stats[g]['lost'] = 0
                row_for(commit,p,'regenerate',stats)
            alive_at[p][str(index)] = {g:[t for t in current[p][g]['tests'] if key(t) in alive[p][g]] for g in b.groups}
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


def screening_exclusions(exp, pools):
    """Exclude rejected test candidates and generator scratch files from policy jobs.

    Successfully compiled helpers and other unscreened siblings stay available
    to javac and the helper inventory, as before. Scratch subtrees are excluded
    because the pool snapshot copies every sibling of a kept test: without this
    the whole-root compile fails at every index and an unparsable attempt can
    abort the per-file fallback. Pool artifacts on disk and their stored hashes
    are not modified; a per-pool summary goes to results/policy/excluded-sources.json.
    """
    excluded = set(); summary = {}
    for (group, index), tests in pools.items():
        root = exp/f'results/policy/pools/{group}/{index}'
        kept = {(exp/t['file']).resolve() for t in tests}
        rejected = set()
        for path in (root/'generated').glob('*/round*/validation/*.json'):
            record = read_json(path)
            candidate = record.get('file')
            helper = not record.get('methods') and (record.get('compile') or {}).get('exit_code') == 0
            if candidate and not helper and (root/candidate).resolve() not in kept:
                rejected.add((root/candidate).resolve())
        scratch = set()
        for source_root in {(exp/t['source_root']).resolve() for t in tests if t.get('source_root')}:
            for name in GENERATOR_SCRATCH_DIRS:
                scratch.update(p.resolve() for p in (source_root/name).rglob('*.java'))
        scratch -= kept
        for p in rejected | scratch:
            excluded.add(str(p.relative_to(exp.resolve())))
        summary[f'{group}/{index}'] = dict(kept=len(kept), rejected=len(rejected), scratch=len(scratch))
    write_json(exp/'results/policy/excluded-sources.json', summary)
    return excluded
