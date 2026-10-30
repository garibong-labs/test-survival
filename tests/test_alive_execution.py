"""Alive-entry execution preserves cumulative results and legacy caches."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_policy import baseline, test_test
from test_survival.adapters import java_worker
from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.adapters.run_reuse import PRE_BOUNDED_COVERAGE, survival_provenance_matches
from test_survival.config import validate
from test_survival.io import read_csv, read_json, write_csv, write_json
from test_survival.policy.baseline import Baseline
from test_survival.policy.config import reusable_run
from test_survival.policy.pipeline import Steps
from test_survival.policy.plan import build_plan
from test_survival.policy.segments import execution_id, key
from test_survival.rules.survival import advance, dead_method
from test_survival.stages import survive


def test_baseline_alive_jobs_preserve_aggregate_bytes_and_reuse_prefix(tmp_path, monkeypatch):
    config = validate(dict(name='alive', anchor='t', project={'repo':str(tmp_path)},
                           range={'skip_untouched':'main-sources'},
                           dev={'groups':[{'name':'dev'}, {'name':'scope', 'derive_from':'dev'}]}))
    methods = [dict(id=n, file='T.java', execution_hash='source', **{'class':'T'}, method=n, kind='plain') for n in 'abc']
    frozen = {'dev':methods, 'scope':methods[1:]}
    write_json(tmp_path/'results/frozen-denominators.json', frozen)
    write_json(tmp_path/'results/anchor.json', dict(sha='anchor', date='date'))
    commits = [dict(index=i, sha=str(i), date='date', subject='change') for i in range(1,8)]
    write_csv(tmp_path/'results/commits.csv', commits, list(commits[0]))
    write_json(tmp_path/'results/commit-paths.json', {str(i):['README.md' if i==2 else 'src/main/java/A.java'] for i in range(1,8)})
    for i in range(1,8):
        write_json(tmp_path/f'results/builds/{i}.json', dict(sha=str(i), build='fail' if i==3 else 'ok'))
    adapter = JavaMavenAdapter(tmp_path, config)
    fingerprint = adapter.run_fingerprint(methods)
    old_provenance = {**adapter.provenance(), **PRE_BOUNDED_COVERAGE}
    death = {'a':1, 'b':4, 'c':6}
    def observe(index, selected):
        # A dead test may even pass again in the old implementation; the
        # cumulative aggregate must still ignore it.
        return [dict(t, state='fail' if index==death[t['id']] else 'pass', flaky=False, message='fixture') for t in selected]
    for row in commits:
        i=row['index']
        if i==3:
            continue
        for group in frozen:
            raw = dict(index=i, sha=str(i), group=group, status='skipped-untouched' if i==2 else 'evaluated',
                       methods=observe(i, frozen[group]), files=[], input_sha256=fingerprint, runner_sha256=old_provenance)
            folder='raw' if group=='dev' else 'raw-derived'
            write_json(tmp_path/f'results/{folder}/{i}-{group}.json', raw)
    survive.aggregate(tmp_path, config)
    outputs = ['survival.csv', 'first-failures.csv', 'alive-entering.json']
    expected = {name:(tmp_path/'results'/name).read_bytes() for name in outputs}
    cached = tmp_path/'results/raw/1-dev.json'; cached_bytes=cached.read_bytes()
    for i in (4,5,6,7):
        (tmp_path/f'results/raw/{i}-dev.json').unlink()
    calls=[]
    monkeypatch.setattr(survive, 'measurement_session', lambda *a: nullcontext())
    def worker(self, job):
        calls.append(deepcopy(job))
        record=dict(index=job['index'], sha=job['sha'], group=job['group'], status='evaluated', files=[],
                    input_sha256=job['input_sha256'], runner_sha256=job['runner_sha256'],
                    methods=observe(job['index'], job['methods'])+job['dead_methods'])
        write_json(self.exp/job['output'], record)
        return record
    monkeypatch.setattr(JavaMavenAdapter, 'worker', worker)
    survive.run(tmp_path, config)
    assert {name:(tmp_path/'results'/name).read_bytes() for name in outputs} == expected
    assert cached.read_bytes() == cached_bytes
    assert [(j['index'], [t['id'] for t in j['methods']]) for j in calls] == [(4,['b','c']), (5,['c']), (6,['c']), (7,[])]
    assert all(j['input_sha256']==fingerprint for j in calls)
    assert calls[1]['dead_methods'][0]['message']=='not executed; failed at index 1'
    assert calls[1]['dead_methods'][1]['message']=='not executed; failed at index 4'
    assert 'dead' not in {r['kind'] for r in read_csv(tmp_path/'results/first-failures.csv')}
    count=len(calls)
    survive.run(tmp_path, config)
    assert len(calls)==count


def test_worker_writes_dead_inventory_without_running_it(tmp_path, monkeypatch):
    monkeypatch.setattr(java_worker, 'EXP', tmp_path, raising=False)
    monkeypatch.setattr(java_worker, 'DEVELOPER', False, raising=False)
    monkeypatch.setattr(java_worker, 'run_file', lambda *a, **kw: pytest.fail('dead test executed'))
    dead = dead_method(dict(id='a', **{'class':'T'}, method='a'), 2)
    java_worker._run_group(dict(index=3, sha='sha', group='dev', phase='A', methods=[], dead_methods=[dead],
                                work='work/test', output='result.json', input_sha256='full', runner_sha256={}),
                           SimpleNamespace(execute=lambda *a: pytest.fail('dead test compiled'), identity='subprocess'))
    record=read_json(tmp_path/'result.json')
    assert record['methods']==[dead] and record['requested_ids']==[] and record['files']==[]
    assert advance(set(), record['methods'])==(set(), [])
    with pytest.raises(ValueError, match='Unknown method state: dead'):
        advance({'a'}, record['methods'])


def test_legacy_provenance_compatibility_remains_strict():
    current={**dict.fromkeys(PRE_BOUNDED_COVERAGE, 'new'), 'MethodLauncher.java':'unchanged'}
    previous={**current, **PRE_BOUNDED_COVERAGE}
    assert survival_provenance_matches(previous, current)
    assert not survival_provenance_matches({**previous, 'MethodLauncher.java':'other'}, current)
    assert not survival_provenance_matches({**previous, 'java_worker.py':'unknown'}, current)
    assert not survival_provenance_matches({}, current)


def test_complete_v2_policy_record_is_reused_byte_for_byte(baseline, monkeypatch):
    from test_survival.policy.execute import Runs
    exp,config,_=baseline; b=Baseline(exp,config); steps=Steps(b,build_plan(b)); runs=Runs(b,steps)
    t=test_test('a'); runs.register([t]); path=runs.path(1,execution_id(t))
    write_json(path,dict(coverage_version=2,coverage_status='complete',methods=[dict(t,state='pass',lines=[])]))
    state=exp/f'state/policy-run-{execution_id(t)}-1.json'
    write_json(state,dict(status='completed',config_hash=steps.digest))
    before={p:p.read_bytes() for p in (path,state)}
    monkeypatch.setattr(runs,'initialize',lambda:pytest.fail('Complete legacy record was remeasured'))
    assert reusable_run(runs.get(1,execution_id(t)))
    assert before=={p:p.read_bytes() for p in before}


def test_policy_alive_jobs_preserve_curves_failures_and_alive_bytes(baseline, monkeypatch):
    from test_survival.policy import execute
    exp,config,_=baseline; b=Baseline(exp,config)
    b.options['set']=[dict(name='reset', trigger='release', action='reset')]
    b.curves.append({**b.curves[-1], 'index':'4', 'sha':'sha4'})
    b.by_index[4]=b.curves[-1]
    plan=dict(points=[dict(group='dev-scope', index=1, trigger='release')], pools=[dict(group='dev-scope', index=1)])
    tests=[dict(test_test(n), execution_hash='shared') for n in 'abc']
    monkeypatch.setattr(execute, 'baseline_tests', lambda _: {'dev-scope':[]})
    monkeypatch.setattr(b, 'raw', lambda *a: {'methods':[]})
    monkeypatch.setattr(JavaMavenAdapter, 'build', lambda *a: {'build':'ok'})
    monkeypatch.setattr(JavaMavenAdapter, 'setup_runner', lambda *a: None)
    calls=[]
    def worker(self, job):
        calls.append(deepcopy(job))
        return dict(methods=[dict(t, state='fail' if (job['index'],t['id']) in ((2,'a'),(3,'b')) else 'pass', lines=[])
                             for t in job['methods']])
    monkeypatch.setattr(JavaMavenAdapter, 'worker', worker)
    run_index=execute.Runs.run_index
    # Model the previous implementation: execute the complete pool each time.
    monkeypatch.setattr(execute.Runs, 'run_index', lambda self,i,ds,**kw: run_index(self,i,ds))
    execute.survive(b,plan,{('dev-scope',1):tests},Steps(b,plan))
    out=exp/'results/policy/reset'
    outputs=['survival.csv','first-failures.csv','alive.json']
    expected={name:(out/name).read_bytes() for name in outputs}
    assert [len(j['methods']) for j in calls]==[3,3,3]
    # Retain the completed prefix, then resume with alive-entry execution.
    prefix=exp/'results/policy/runs/shared/2.json'
    saved=read_json(prefix); saved['coverage_version']=2; write_json(prefix,saved)
    prefix_bytes=prefix.read_bytes()
    for i in (3,4):
        (exp/f'results/policy/runs/shared/{i}.json').unlink()
        (exp/f'state/policy-run-shared-{i}.json').unlink()
    calls.clear(); monkeypatch.setattr(execute.Runs, 'run_index', run_index)
    execute.survive(b,plan,{('dev-scope',1):tests},Steps(b,plan))
    assert {name:(out/name).read_bytes() for name in outputs}==expected
    assert prefix.read_bytes()==prefix_bytes
    assert [(j['index'],[t['id'] for t in j['methods']]) for j in calls]==[(3,['b','c']),(4,['c'])]
    result=read_json(exp/'results/policy/runs/shared/4.json')
    assert {t['id']:t['state'] for t in result['methods']}=={'a':'dead','b':'dead','c':'pass'}
    assert [t['message'] for t in result['methods'] if t['state']=='dead']==[
        'not executed; failed at index 2','not executed; failed at index 3']


def test_policy_shared_method_runs_if_any_policy_revives_it(baseline, monkeypatch):
    from test_survival.policy import execute
    exp,config,_=baseline; b=Baseline(exp,config)
    b.options['set']=[dict(name='early',trigger='release',action='reset'), dict(name='later',trigger='threshold',action='reset')]
    plan=dict(points=[dict(group='dev-scope',index=1,trigger='release'),dict(group='dev-scope',index=2,trigger='threshold')],
              pools=[dict(group='dev-scope',index=i) for i in (1,2)])
    t=test_test('shared')
    monkeypatch.setattr(execute,'baseline_tests',lambda _: {'dev-scope':[]})
    monkeypatch.setattr(b,'raw',lambda *a: {'methods':[]})
    monkeypatch.setattr(JavaMavenAdapter,'build',lambda *a: {'build':'ok'})
    monkeypatch.setattr(JavaMavenAdapter,'setup_runner',lambda *a: None)
    calls=[]
    def worker(self,job):
        calls.append(job)
        return dict(methods=[dict(m,state='pass',lines=[]) for m in job['methods']])
    monkeypatch.setattr(JavaMavenAdapter,'worker',worker)
    # The cached measurement killed it for the early policy at index 2.
    # The later policy reintroduces the same physical method at that index.
    step=Steps(b,plan); digest=execution_id(t)
    write_json(exp/f'results/policy/runs/{digest}/2.json',dict(coverage_version=2,coverage_status='complete',methods=[dict(t,state='fail',lines=[])]))
    write_json(exp/f'state/policy-run-{digest}-2.json',dict(status='completed',config_hash=step.digest))
    execute.survive(b,plan,{('dev-scope',i):[t] for i in (1,2)},step)
    assert [(j['index'],[m['id'] for m in j['methods']]) for j in calls]==[(3,['shared'])]
    assert read_csv(exp/'results/policy/early/survival.csv')[-1]['dev-scope_alive']=='0'
    assert read_csv(exp/'results/policy/later/survival.csv')[-1]['dev-scope_alive']=='1'


def test_partial_pool_executes_only_alive_selected_members(baseline, monkeypatch):
    from test_survival.policy.execute import Runs
    exp,config,_=baseline; b=Baseline(exp,config); runs=Runs(b,Steps(b,build_plan(b)))
    tests=[dict(test_test(n),execution_hash='shared') for n in ('alive','died','unused')]
    runs.register(tests); runs.ready=True; jobs=[]
    monkeypatch.setattr(runs.adapter,'build',lambda *a: {'build':'ok'})
    def worker(job):
        jobs.append(job)
        return dict(methods=[dict(t,state='pass',lines=[]) for t in job['methods']])
    monkeypatch.setattr(runs.adapter,'worker',worker)
    runs.run_index(2,{'shared'},alive={key(tests[0])},dead_at={key(tests[1]):1})
    result=runs.get(2,'shared')
    assert len(jobs)==1 and [t['id'] for t in jobs[0]['methods']]==['alive']
    assert {t['id']:t['state'] for t in result['methods']}=={'alive':'pass','died':'dead','unused':'not-selected'}
    assert result['coverage_status']=='complete'


@pytest.mark.parametrize('action', ['reset', 'fill'])
@pytest.mark.parametrize('cached_marker', [False, True, 'mixed'])
def test_policy_remeasures_revived_baseline_dead_method(baseline, monkeypatch, action, cached_marker):
    from test_survival.policy import execute
    exp,config,_=baseline; b=Baseline(exp,config)
    b.options['set']=[dict(name='revive', trigger='release', action=action)]
    t=dict(test_test('shared'), execution_hash='same-source', origin_id='baseline-id')
    extra=dict(test_test('extra'), execution_hash='same-source')
    pool=[t,extra] if cached_marker=='mixed' else [t]
    plan=dict(points=[dict(group='dev-scope',index=2,trigger='release')],
              pools=[dict(group='dev-scope',index=2)])
    monkeypatch.setattr(execute,'baseline_tests',lambda _: {'dev-scope':[t]})
    def raw(index, group):
        observed=dict(t,id=t['origin_id'],state='fail',flaky=False,lines=[])
        return {'methods':[observed if index==1 else dead_method(observed,1)]}
    monkeypatch.setattr(b,'raw',raw)
    monkeypatch.setattr(JavaMavenAdapter,'build',lambda *a: {'build':'ok'})
    monkeypatch.setattr(JavaMavenAdapter,'setup_runner',lambda *a: None)
    jobs=[]
    def worker(self,job):
        jobs.append(job)
        return dict(methods=[dict(m,state='pass',lines=[]) for m in job['methods']])
    monkeypatch.setattr(JavaMavenAdapter,'worker',worker)
    step=Steps(b,plan)
    if cached_marker:
        # The old importer can have written a completed run before advance()
        # rejected its dead marker. Resume must repair that cached import too.
        write_json(exp/'results/policy/runs/same-source/3.json',
                   dict(coverage_version=3,coverage_status='complete',imported=cached_marker!='mixed',
                        methods=[dead_method(t,1)]+([dict(extra,state='pass',lines=[])] if cached_marker=='mixed' else [])))
        write_json(exp/'state/policy-run-same-source-3.json',dict(status='completed',config_hash=step.digest))
    execute.survive(b,plan,{('dev-scope',2):pool},step)
    assert [(job['index'],[m['id'] for m in job['methods']]) for job in jobs]==[(3,[m['id'] for m in pool])]
    assert read_csv(exp/'results/policy/revive/survival.csv')[-1]['dev-scope_alive']==str(len(pool))
    result=read_json(exp/'results/policy/runs/same-source/3.json')
    assert not result['imported'] and result['methods'][0]['state']=='pass'
    execute.survive(b,plan,{('dev-scope',2):pool},step)
    assert len(jobs)==1


@pytest.mark.parametrize('imported', [False, True])
@pytest.mark.parametrize('revived', ['died', 'unused'])
def test_policy_cache_markers_follow_current_demand(baseline, monkeypatch, imported, revived):
    from test_survival.policy.execute import Runs
    exp,config,_=baseline; b=Baseline(exp,config); step=Steps(b,build_plan(b))
    tests={n:dict(test_test(n),execution_hash='shared') for n in ('alive','died','unused')}
    path=exp/'results/policy/runs/shared/2.json'
    state=exp/'state/policy-run-shared-2.json'
    write_json(path,dict(coverage_version=3,coverage_status='complete',imported=imported,
                        methods=[dict(tests['alive'],state='pass',lines=[]),dead_method(tests['died'],1),
                                 dict(tests['unused'],state='not-selected',lines=[])]))
    write_json(state,dict(status='completed',config_hash=step.digest))
    before={p:p.read_bytes() for p in (path,state)}
    jobs=[]
    monkeypatch.setattr(JavaMavenAdapter,'build',lambda *a: {'build':'ok'})
    def worker(self,job):
        jobs.append(job)
        return dict(methods=[dict(t,state='pass',lines=[]) for t in job['methods']])
    monkeypatch.setattr(JavaMavenAdapter,'worker',worker)
    def run(alive):
        runs=Runs(b,step); runs.ready=True; runs.register(tests.values())
        runs.run_index(2,{'shared'},alive={key(tests[n]) for n in alive},dead_at={key(tests['died']):1})
        return runs.get(2,'shared')
    run({'alive'})
    assert not jobs and before=={p:p.read_bytes() for p in before}
    # Available baseline observations for inactive methods do not constitute
    # live policy demand, and must not invalidate legitimate cached markers.
    runs=Runs(b,step); runs.register(tests.values())
    monkeypatch.setattr(runs,'imports',lambda *a: {f'A#{n}':dict(t,state='pass',lines=[]) for n,t in tests.items()})
    runs.run_index(2,{'shared'},alive={key(tests['alive'])},dead_at={key(tests['died']):1})
    runs.get(2,'shared')
    assert not jobs and before=={p:p.read_bytes() for p in before}
    result=run({'alive',revived})
    assert len(jobs)==1 and {t['id'] for t in jobs[0]['methods']}=={'alive',revived}
    assert {t['id'] for t in result['methods'] if t['state']=='pass'}=={'alive',revived}
    remaining='unused' if revived=='died' else 'died'
    assert next(t for t in result['methods'] if t['id']==remaining)['state']==('not-selected' if remaining=='unused' else 'dead')
    run({'alive',revived})
    assert len(jobs)==1


@pytest.mark.parametrize('cached_state', ['dead', 'pass'])
def test_baseline_skip_change_revalidates_cached_dead_methods(tmp_path, monkeypatch, cached_state):
    config=validate(dict(name='skip-change',anchor='t',project={'repo':str(tmp_path)},
                         range={'skip_untouched':'none'},dev={'groups':[{'name':'dev'}]}))
    t=dict(id='a',file='T.java',execution_hash='source',**{'class':'T'},method='a')
    write_json(tmp_path/'results/frozen-denominators.json',{'dev':[t]})
    write_json(tmp_path/'results/anchor.json',dict(sha='anchor',date='date'))
    commits=[dict(index=i,sha=str(i),date='date',subject='change') for i in (1,2)]
    write_csv(tmp_path/'results/commits.csv',commits,list(commits[0]))
    write_json(tmp_path/'results/commit-paths.json',{'1':['README.md'],'2':['src/main/java/A.java']})
    adapter=JavaMavenAdapter(tmp_path,config); fingerprint=adapter.run_fingerprint([t])
    for i in (1,2):
        write_json(tmp_path/f'results/builds/{i}.json',dict(sha=str(i),build='ok'))
        method=(dead_method(t,1) if cached_state=='dead' else dict(t,state='pass',flaky=False)) if i==2 else dict(t,state='fail',flaky=False)
        write_json(tmp_path/f'results/raw/{i}-dev.json',dict(index=i,sha=str(i),group='dev',status='evaluated',
                   methods=[method],files=[],input_sha256=fingerprint,runner_sha256=adapter.provenance()))
    calls=[]
    monkeypatch.setattr(survive,'measurement_session',lambda *a: nullcontext())
    def worker(self,job):
        calls.append(job)
        record=dict(index=job['index'],sha=job['sha'],group='dev',status='evaluated',files=[],
                    methods=[dict(m,state='pass',flaky=False) for m in job['methods']]+job['dead_methods'],
                    input_sha256=job['input_sha256'],runner_sha256=job['runner_sha256'])
        write_json(self.exp/job['output'],record)
        return record
    monkeypatch.setattr(JavaMavenAdapter,'worker',worker)
    path=tmp_path/'results/raw/2-dev.json'; original=path.read_bytes()
    survive.run(tmp_path,config)
    assert not calls and path.read_bytes()==original  # Still dead: reuse unchanged.
    config['range']['skip_untouched']='main-sources'
    assert JavaMavenAdapter(tmp_path,config).run_fingerprint([t])==fingerprint
    survive.run(tmp_path,config)
    assert [(j['index'],[m['id'] for m in j['methods']]) for j in calls]==([(2,['a'])] if cached_state=='dead' else [])
    assert read_csv(tmp_path/'results/survival.csv')[-1]['dev_alive']=='1'
    assert read_json(tmp_path/'results/alive-entering.json')['2']['dev']==['a']
    assert read_csv(tmp_path/'results/first-failures.csv')==[]
    if cached_state=='pass':
        assert path.read_bytes()==original  # Fully executed legacy observations stay reusable.
