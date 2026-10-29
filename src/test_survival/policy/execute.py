"""Commit execution deduplicated across policies by source hash and index."""
from collections import defaultdict

from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json, write_json, write_csv
from .artifacts import snapshot
from .segments import key, identity, segment, regenerate, assemble, evaluate


class Runs:
    def __init__(self, baseline, step):
        self.b = baseline
        self.step = step
        self.adapter = JavaMavenAdapter(baseline.exp, baseline.execution_config)
        self.ready = False
        self.needed = set()
        self.catalog = defaultdict(dict)
        self.baseline_catalog = {}
        self.pool_catalog = defaultdict(dict)

    def initialize(self):
        if self.ready:
            return
        write_json(self.b.exp/'results/anchor.json', self.b.anchor)
        if self.adapter.build(0, self.b.anchor['sha'])['build'] != 'ok':
            raise RuntimeError('Policy anchor build failed')
        self.adapter.setup_runner()
        self.ready = True

    def register(self, tests):
        for t in tests:
            self.catalog[t['file_hash']][identity(t)] = t

    def get(self, index, digest):
        self.needed.add((digest,index))
        path = self.b.exp/f'results/policy/runs/{digest}/{index}.json'
        methods = list(self.catalog[digest].values())
        # A physical source is compiled once even when policies reference different copies.
        source = methods[0]
        methods = [{**t, 'file':source['file'], **{k:source[k] for k in ('source_root','resource_root') if k in source}} for t in methods]
        def execute():
            imported = dict(self.pool_catalog.get((index,digest), {}))
            for group, baseline_tests in self.baseline_catalog.items():
                candidates = [t for t in baseline_tests if t['file_hash'] == digest]
                if candidates:
                    raw = {r['id']:r for r in self.b.raw(index,group)['methods']}
                    imported.update({identity(t):raw[t['origin_id']] for t in candidates})
            if set(imported) >= {identity(t) for t in methods}:
                write_json(path, dict(index=index, sha=self.b.by_index[index]['sha'], status='evaluated', imported=True,
                                      methods=[imported[identity(t)] for t in methods]))
                return
            self.initialize()
            sha = self.b.by_index[index]['sha']
            build = self.adapter.build(index, sha)
            if build['build'] != 'ok':
                raise RuntimeError('Build differs from baseline at evaluable index '+str(index))
            # Source roots carry developer helper sources without treating the entire frozen suite as a new job.
            result = self.adapter.run_group(index, 'policy-'+digest, methods)
            if any(r.get('exit_code') in (-9,137) for f in result['files'] for r in [f['compile'],*f['runs']]):
                raise RuntimeError('Policy worker killed; run is incomplete')
            write_json(path, result)
        self.step(f'run-{digest}-{index}', execute, outputs=[path])
        result = read_json(path)
        if {identity(t) for t in methods} != {identity(t) for t in result['methods']}:
            raise ValueError('Shared run method inventory changed; use --force')
        return result

    def carry(self, index, digest):
        self.needed.add((digest,index))
        path = self.b.exp/f'results/policy/runs/{digest}/{index}.json'
        status = 'skipped-untouched' if index in self.b.skipped else 'build-fail'
        self.step(f'run-{digest}-{index}', lambda:write_json(path, dict(index=index, sha=self.b.by_index[index]['sha'], status=status, methods=[])), outputs=[path])


def baseline_tests(b):
    groups = {}
    generators = {g['name']:g['method'] for g in b.config['generators']}
    for g in b.groups:
        groups[g] = snapshot(b.path, b.exp, b.frozen[g], group=g, method=generators.get(g,'dev'),
                             scope=b.scope['classes'], prefix='results/policy/baseline-sources')
    return groups


def survive(b, plan, pools, step):
    frozen = baseline_tests(b)
    runs = Runs(b, step)
    runs.baseline_catalog = frozen
    for (_,index), tests in pools.items():
        for t in tests:
            runs.pool_catalog[index,t['file_hash']][identity(t)] = dict(t,state='pass',flaky=False)
    for tests in [*frozen.values(), *pools.values()]:
        runs.register(tests)
    policies = {p['name']:p for p in b.options['set']}
    current = {p:{g:segment(1,0,tests,[]) for g,tests in frozen.items()} for p in policies}
    segments = {p:{g:[s] for g,s in gs.items()} for p,gs in current.items()}
    alive = {p:{g:{key(t) for t in s['tests']} for g,s in gs.items()} for p,gs in current.items()}
    points = {p:{(x['group'],x['index']) for x in plan['points'] if x['trigger']==policy['trigger'] and x['index'] is not None} for p,policy in policies.items()}
    curves = {p:[] for p in policies}; failures = {p:[] for p in policies}; alive_at = {p:{} for p in policies}
    def row_for(commit, p, event, stats):
        row = {k:commit[k] for k in ('index','sha','date','subject','evaluated')}
        row['event'] = event
        for g in b.groups:
            row.update({g+'_'+k:v for k,v in stats[g].items()})
        curves[p].append(row)
    for commit in b.curves:
        index = int(commit['index']); evaluable = index > 0 and b.evaluable(index)
        for p, policy in policies.items():
            stats = {}
            for g in b.groups:
                s = current[p][g]; observations = []
                if evaluable:
                    if s['segment'] == 1:
                        raw = b.raw(index,g)
                        by_id = {r['id']:r for r in raw['methods']}
                        observations = [{**by_id[t['origin_id']], 'id':key(t)} for t in s['tests']]
                    else:
                        observations = assemble(s['tests'], {h:runs.get(index,h) for h in sorted({t['file_hash'] for t in s['tests']})})
                if index and not evaluable and s['segment'] > 1:
                    for digest in {t['file_hash'] for t in s['tests']}:
                        runs.carry(index,digest)
                alive[p][g], lost, stats[g] = evaluate(s, alive[p][g], observations, evaluated=evaluable)
                failures[p].extend(dict(index=index,sha=commit['sha'],group=g,segment=s['segment'],id=t['id'],kind=t['state'],flaky=t.get('flaky',False)) for t in lost)
            row_for(commit,p,'evaluate',stats)
            triggered = [g for g in b.groups if (g,index) in points[p]]
            if triggered:
                for g in triggered:
                    s = regenerate(current[p][g],alive[p][g],pools[(g,index)],action=policy['action'],developer=g in b.dev,seed=b.options['fill_seed'],index=index)
                    for digest in sorted({t['file_hash'] for t in s['tests']}):
                        runs.get(index,digest)
                    current[p][g] = s; segments[p][g].append(s); alive[p][g] = {key(t) for t in s['tests']}
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
    write_json(b.exp/'results/policy/needed-runs.json',[dict(file_hash=h,index=i) for h,i in sorted(runs.needed)])
