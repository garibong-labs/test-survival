"""Ordered policy decisions over shared physical executions.

A logical row belongs to a policy/group/segment. Physical runs never contain
selection carries and can satisfy multiple logical consumers at the same SHA.
"""
from collections import defaultdict

from ..io import read_json, write_json, write_csv
from ..selection import Selector, carry, executed, counts
from ..rules.coverage import usable_lines
from ..rules.survival import dead_method
from ..work import release_checkout
from .config import active_plan, regenerates, COVERAGE_VERSION
from .segments import execution_id, identity, key, segment, regenerate, evaluate
from .execute import Runs, baseline_tests, generator_methods, screening_exclusions, settle


def logical_path(b, policy, group, number, index):
    return b.exp/f'results/policy/{policy}/decisions/{group}/{number}/{index}.json'


class Decisions:
    def __init__(self, b, runs):
        self.b, self.runs = b, runs
        self.selector = Selector(runs.adapter)
        self.history = {}
        self.phase = defaultdict(int)

    def physical(self, index, wanted, context):
        """Reuse same-index actual executions; repair only unusable observations."""
        observed = {}; missing = []; old = {}
        sha = self.b.by_index[index]['sha']
        for k,t in wanted.items():
            raw = read_json(self.runs.path(index, execution_id(t)), {})
            row = next((r for r in raw.get('methods', []) if identity(r) == identity(t)), None)
            if row and raw.get('sha') == sha:
                old[k] = row
                if row.get('selection_action') == 'executed' and row.get('dependency_record') and usable_lines(row):
                    observed[k] = row
                    continue
            # Matching baseline actual executions can initialize a boundary;
            # a baseline selection carry cannot initialize the new segment.
            for g in self.b.groups:
                baseline = self.b.raw(index, g)
                candidate = next((r for r in baseline.get('methods', []) if r.get('execution_hash') == execution_id(t)
                                  and identity(r) == identity(t)), None)
                if (candidate and baseline.get('sha') == sha and candidate.get('selection_action') == 'executed'
                        and candidate.get('dependency_record') and usable_lines(candidate)):
                    observed[k] = {**t, **candidate, 'id':t['id']}
                    break
            if k not in observed: missing.append(t)
        if missing:
            self.runs.initialize()
            suffix = self.phase[index]; self.phase[index] += 1
            batch = self.runs.adapter.worker(dict(action='policy-survive', index=index, sha=sha, group='policy-survival',
                        methods=missing, scope_files=[c['file'] for c in self.b.scope['classes']],
                        scope_classes=[c['class'] for c in self.b.scope['classes']],
                        excluded_files=sorted(self.runs.excluded_for(missing)),
                        output=f'results/policy/batches/{index}-affected-{suffix}.json',
                        work=f'work/policy-survive/{index}-affected-{suffix}'))
            for row in batch['methods']:
                k = key(row)
                verdict = {n:old[k][n] for n in ('state','message','flaky') if n in old[k]} if k in old else None
                observed[k] = executed(settle({**wanted[k], **row}, verdict, []), index, sha, context)
        if set(observed) != set(wanted): raise ValueError('Incomplete affected policy physical batch')
        grouped = defaultdict(list)
        for k,t in wanted.items(): grouped[execution_id(t)].append(observed[k])
        for digest, rows in grouped.items():
            path = self.runs.path(index, digest)
            prior = read_json(path, {})
            combined = {identity(r):r for r in prior.get('methods', [])} if prior.get('sha') == sha else {}
            combined.update({identity(r):r for r in rows})
            record = dict(index=index, sha=sha, status='evaluated', coverage_version=COVERAGE_VERSION,
                          coverage_status='complete' if all(usable_lines(r) for r in combined.values()) else 'missing',
                          methods=list(combined.values()))
            write_json(path, record)
            self.runs.needed.add((digest,index))
        return observed

    def measure(self, index, consumers, *, boundary=False):
        """consumers: (policy, group, segment, alive method keys)."""
        if not consumers: return {}
        sha = self.b.by_index[index]['sha']
        self.runs.initialize()
        context = self.selector.context(index, sha)
        wanted = {}; decisions = {}; previous = {}
        for p,g,s,alive in consumers:
            slot = (p,g,s['segment'])
            path = logical_path(self.b,p,g,s['segment'],index)
            saved = read_json(path, {})
            saved_rows = {key(r):r for r in saved.get('methods', [])} if saved.get('sha') == sha else {}
            rows = {}; previous[slot] = saved_rows
            for t in s['tests']:
                k = key(t)
                if k not in alive:
                    rows[k] = dead_method(t,index)
                    continue
                prior = self.history.get((*slot,k))
                if not boundary and not self.selector.decision(prior,index,sha,context,start=s['start']):
                    rows[k] = carry(prior)
                else:
                    wanted[k] = t
            decisions[slot] = rows
        physical = self.physical(index,wanted,context) if wanted else {}
        result = {}
        for p,g,s,alive in consumers:
            slot = (p,g,s['segment']); rows = decisions[slot]
            for t in s['tests']:
                k = key(t)
                if k not in rows:
                    row = physical[k]
                    prior = previous[slot].get(k)
                    verdict = {n:prior[n] for n in ('state','message','flaky') if n in prior} if prior else None
                    rows[k] = settle({**t, **row}, verdict, [])
                rows[k] = {**rows[k], 'id':key(t)}
                if k in alive: self.history[(*slot,k)] = rows[k]
            record = dict(index=index,sha=sha,segment=s['segment'],selection='affected',methods=list(rows.values()),
                          selection_counts=counts(rows.values()))
            write_json(logical_path(self.b,p,g,s['segment'],index),record,compress=True)
            result[p,g] = record['methods']
        return result


def survive(b, plan, pools, step):
    plan = active_plan(b,plan)
    pools = {k:v for k,v in pools.items() if any(p['group'] == k[0] and p['index'] == k[1] for p in plan['pools'])}
    frozen = baseline_tests(b); runs = Runs(b,step)
    runs.excluded = screening_exclusions(b.exp,pools,frozen,b.path,generator_methods(b))
    for tests in [*frozen.values(),*pools.values()]: runs.register(tests)
    decisions = Decisions(b,runs)
    policies = {p['name']:p for p in b.options['set']}
    current = {p:{g:segment(1,0,ts,[]) for g,ts in frozen.items()} for p in policies}
    segments = {p:{g:[s] for g,s in gs.items()} for p,gs in current.items()}
    alive = {p:{g:{key(t) for t in s['tests']} for g,s in gs.items()} for p,gs in current.items()}
    points = {p:{(x['group'],x['index']) for x in plan['points'] if x['trigger']==policy['trigger']
                 and x['index'] is not None and regenerates(b,x['group'],policy)} for p,policy in policies.items()}
    curves = defaultdict(list); failures = defaultdict(list); alive_at = defaultdict(dict)
    missing = defaultdict(dict); ledger = defaultdict(list)
    def emit(commit,p,event,stats):
        row = {k:commit[k] for k in ('index','sha','date','subject','evaluated')}; row['event'] = event
        for g in b.groups: row.update({g+'_'+k:v for k,v in stats[g].items()})
        curves[p].append(row)
    def record(p,g,index,rows,event):
        useful = [r for r in rows if r['state'] not in ('dead','not-selected')]
        ledger[p].append(dict(index=index,group=g,event=event,**counts(useful)))
        if any(not usable_lines(r) for r in useful): missing[p][index,g] = 'missing usable per-test coverage'
    for commit in b.curves:
        index = int(commit['index']); evaluable = index > 0 and b.evaluable(index)
        consumers = [(p,g,s,alive[p][g]) for p,gs in current.items() for g,s in gs.items() if s['segment'] > 1]
        measured = decisions.measure(index,consumers) if evaluable else {}
        regenerated = []
        for p,policy in policies.items():
            stats = {}
            for g in b.groups:
                s = current[p][g]; observations = []
                if evaluable:
                    if s['segment'] == 1:
                        raw = {r['id']:r for r in b.raw(index,g)['methods']}
                        observations = [{**t,**raw[t['origin_id']],'id':key(t)} for t in s['tests']]
                    else: observations = measured[p,g]
                    record(p,g,index,[r for r in observations if r['id'] in alive[p][g]],'evaluate')
                alive[p][g],lost,stats[g] = evaluate(s,alive[p][g],observations,evaluated=evaluable)
                failures[p].extend(dict(index=index,sha=commit['sha'],group=g,segment=s['segment'],id=t['id'],kind=t['state'],flaky=t.get('flaky',False)) for t in lost)
            emit(commit,p,'evaluate',stats)
            triggered = [g for g in b.groups if (g,index) in points[p]]
            for g in triggered:
                s = regenerate(current[p][g],alive[p][g],pools[g,index],action=policy['action'],developer=g in b.dev,seed=b.options['fill_seed'],index=index)
                current[p][g] = s; segments[p][g].append(s); alive[p][g] = {key(t) for t in s['tests']}
                regenerated.append((p,g,s,alive[p][g]))
                _,_,stats[g] = evaluate(s,alive[p][g],[],evaluated=False)
            if triggered:
                for g in b.groups: stats[g]['lost'] = 0
                emit(commit,p,'regenerate',stats)
            alive_at[p][str(index)] = {g:[t for t in current[p][g]['tests'] if key(t) in alive[p][g]] for g in b.groups}
        if regenerated:
            boundary = decisions.measure(index,regenerated,boundary=True)
            for p,g,_,_ in regenerated: record(p,g,index,boundary[p,g],'regenerate')
        release_checkout(b.exp,b.execution_config,index)
    baseline_alive = {g:{key(t) for t in ts} for g,ts in frozen.items()}; baseline_at = {}
    for commit in b.curves:
        index = int(commit['index'])
        if index and b.evaluable(index):
            for g,ts in frozen.items():
                raw = {r['id']:r for r in b.raw(index,g)['methods']}
                baseline_alive[g] -= {key(t) for t in ts if raw[t['origin_id']]['state'] != 'pass'}
        baseline_at[str(index)] = {g:[t for t in ts if key(t) in baseline_alive[g]] for g,ts in frozen.items()}
    write_json(b.exp/'results/policy/none/alive.json',baseline_at)
    for p in policies:
        directory = b.exp/'results/policy'/p
        write_json(directory/'segments.json',segments[p]); write_json(directory/'alive.json',alive_at[p])
        write_csv(directory/'survival.csv',curves[p],list(curves[p][0]))
        write_csv(directory/'first-failures.csv',failures[p],['index','sha','group','segment','id','kind','flaky'])
        write_csv(directory/'coverage-missing.csv',[dict(index=i,sha=b.by_index[i]['sha'],group=g,reason=r) for (i,g),r in sorted(missing[p].items())],['index','sha','group','reason'])
        write_json(directory/'selection-counts.json',ledger[p])
        write_json(directory/'final-summary.json',dict(selection='affected',selection_totals={g:{a:sum(r[a] for r in ledger[p] if r['group']==g) for a in ('executed','carried')} for g in b.groups}))
    write_json(b.exp/'results/policy/needed-runs.json',[dict(execution_hash=h,index=i) for h,i in sorted(runs.needed)])
