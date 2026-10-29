from copy import deepcopy
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from test_survival.config import ConfigError, validate, stage_hash
from test_survival.io import read_csv, read_json, write_json, write_csv
from test_survival.policy.baseline import Baseline
from test_survival.policy.plan import release_index, threshold_indices, next_evaluable, build_plan, run as plan
from test_survival.policy.segments import key, segment, regenerate, evaluate, assemble
from test_survival.policy.pipeline import Steps


def test_release_maps_merged_tag_to_first_containing_first_parent():
    commits = [dict(index=i,sha=str(i)) for i in range(1,5)]
    assert release_index(commits,'side-tag',lambda tag,sha:int(sha)>=3) == 3
    assert release_index(commits,'future',lambda *_:False) is None


def test_threshold_is_cumulative_inclusive_and_capped():
    curves = [dict(index=i,g_alive=n) for i,n in enumerate([10,9,6,5,4,2,0])]
    assert threshold_indices(curves,'g',10,.5) == [3]
    assert threshold_indices(curves,'g',10,.5,2) == [3,5]
    assert threshold_indices(curves,'g',10,.5,0) == []
    assert threshold_indices(curves[:3],'g',10,.5) == []
    assert threshold_indices([dict(index=0,g_alive=0)],'g',0,.5) == []


def test_move_to_next_evaluable_or_no_point():
    assert next_evaluable(2,[1,2,3,4],lambda i:i==4) == 4
    assert next_evaluable(4,[1,2,3,4],lambda i:False) is None


def test_test(name, unit='A#m()', version='old'):
    return dict(id=name, file_hash=version+name, **{'class':'A'}, method=name, unit=unit, file=name+'.java',kind='plain')

# This is fixture data, not a pytest test.
test_test.__test__ = False


def test_reset_and_llm_fill_are_segment_fixed_and_seeded():
    original = [test_test('a'),test_test('b'),test_test('c','B#m()')]
    s = segment(1,0,original,[])
    alive = {key(original[0]),key(original[2])}
    pool = [test_test(str(i),version='new') for i in range(20)]+[test_test('unused','B#m()','new')]
    reset = regenerate(s,alive,pool,action='reset',developer=False,seed=1,index=10)
    assert reset['segment_denominator'] == 21 and reset['carried'] == []
    fill = regenerate(s,alive,pool,action='fill',developer=False,seed=1,index=10)
    again = regenerate(s,alive,list(reversed(pool)),action='fill',developer=False,seed=1,index=10)
    assert fill == again and fill['segment_denominator'] == 3
    assert len(fill['new']) == 1 and all(t['method']!='unused' for t in fill['tests'])
    assert regenerate(s,alive,[],action='fill',developer=False,seed=1,index=10)['segment_denominator'] == 2


def test_dev_fill_keeps_survivor_source_and_adds_every_other_id():
    original = [test_test('a'),test_test('b')]
    s = segment(1,0,original,[])
    pool = [test_test(n,version='new') for n in ['a','b','c','d']]
    filled = regenerate(s,{key(original[0])},pool,action='fill',developer=True,seed=1,index=3)
    assert filled['segment_denominator'] == 4
    assert filled['tests'][0]['file_hash'] == 'olda'
    assert {t['method'] for t in filled['tests']} == {'a','b','c','d'}
    assert len(filled['new']) == 3


def test_segment_cumulative_survival_and_shared_run_assembly():
    tests = [test_test('a'),test_test('b')]; s = segment(1,0,tests,[])
    runs = {t['file_hash']:dict(methods=[dict(t,state='fail' if t['method']=='a' else 'pass')]) for t in tests}
    methods = assemble(tests,runs)
    alive,lost,stats = evaluate(s,{key(t) for t in tests},methods)
    assert stats['alive'] == 1 and stats['lost'] == 1 and stats['carried_alive'] == 1
    for r in methods:r['state']='pass'
    alive,lost,stats = evaluate(s,alive,methods)
    assert stats['alive'] == 1 and stats['lost'] == 0
    new = regenerate(s,alive,tests,action='reset',developer=False,seed=0,index=2)
    assert evaluate(new,{key(t) for t in tests},[],False)[2]['alive'] == 2
    with pytest.raises(ValueError,match='Missing'):
        assemble(tests,{t['file_hash']:dict(methods=[]) for t in tests})


@pytest.fixture
def baseline(tmp_path):
    repo = tmp_path/'repo'; repo.mkdir()
    def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
    git('init','-q'); git('config','user.email','fixture@example.com'); git('config','user.name','Fixture')
    source = repo/'src/main/java/A.java'; source.parent.mkdir(parents=True); source.write_text('class A {}')
    git('add','.'); git('commit','-qm','anchor'); anchor = git('rev-parse','HEAD')
    commits = []
    for i in range(1,4):
        source.write_text('class A {'+' '*i+'}')
        git('add','.'); git('commit','-qm',str(i)); sha = git('rev-parse','HEAD')
        commits.append(dict(index=i,sha=sha,date='2026-01-01',subject=str(i)))
        if i==2:git('tag','release')
    root = repo/'baseline'; exp = repo/'policy'
    config = dict(name='fixture',project=dict(repo=str(repo)),anchor=anchor,dev=dict(groups=[dict(name='dev-scope',denominator='scope-targeted')]))
    fallback = repo/'baseline.yaml'; fallback.write_text(yaml.safe_dump(config))
    config['policies'] = dict(baseline='baseline',baseline_config='baseline.yaml',groups=['dev-scope'],releases=['release'],set=[dict(name='release-reset',trigger='release',action='reset'),dict(name='half-fill',trigger='threshold',action='fill')])
    config = validate(config)
    t = dict(file='dev/src-test-t/java/ATest.java',id='test-id',**{'class':'ATest'},method='works',kind='plain')
    write_json(root/'results/frozen-denominators.json',{'dev-scope':[t]})
    write_json(root/'results/anchor.json',dict(sha=anchor,date='2026-01-01'))
    write_json(root/'population/scope.json',dict(classes=[dict(file='src/main/java/A.java',**{'class':'A'})]))
    write_csv(root/'results/commits.csv',commits,list(commits[0]))
    curves = [dict(index=0,sha=anchor,date='2026-01-01',subject='anchor',evaluated='yes',**{'dev-scope_alive':1,'dev-scope_lost':0})]
    for r in commits:
        curves.append(dict(r,evaluated='yes',**{'dev-scope_alive':int(r['index']<2),'dev-scope_lost':int(r['index']==2)}))
        write_json(root/f'results/raw/{r["index"]}-dev-scope.json',dict(status='evaluated',methods=[dict(t,state='pass' if r['index']<2 else 'fail')]))
    write_csv(root/'results/survival.csv',curves,list(curves[0]))
    write_csv(root/'results/changed-lines.csv',[],['index'])
    write_csv(root/'results/first-failures.csv',[],['index'])
    (root/t['file']).parent.mkdir(parents=True); (root/t['file']).write_text('class ATest {}')
    return exp,config,root


def test_pre_framework_baseline_recomputes_only_in_destination(baseline):
    exp,config,root = baseline
    before = {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    result = plan(exp,config)
    assert result['denominators'] == {'dev-scope':1}
    assert len(result['pools']) == 1 # release and threshold share the same pool
    assert result['pools'][0]['index'] == 2
    assert read_json(exp/'results/commit-paths.json')['1'] == ['src/main/java/A.java']
    assert read_csv(exp/'results/skipped-commits.csv') == []
    assert before == {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_pre_framework_rejects_incomplete_raw_grid(baseline):
    exp,config,root = baseline
    (root/'results/raw/3-dev-scope.json').unlink()
    with pytest.raises(ConfigError,match='raw records'):
        Baseline(exp,config)


def test_baseline_write_and_symlink_guard(baseline):
    exp,config,root = baseline
    with pytest.raises(ConfigError,match='read-only'):
        Baseline(root/'nested',config)
    exp.mkdir(); (exp/'results').symlink_to(root/'results')
    with pytest.raises(ConfigError,match='read-only'):
        Baseline(exp,config)


def test_finished_steps_resume_and_changed_inputs_fail(baseline):
    exp,config,_ = baseline
    b = Baseline(exp,config); step = Steps(b,build_plan(b)); calls=[]
    step('pool',lambda:calls.append(1)); step('pool',lambda:calls.append(2))
    assert calls == [1]
    config['policies']['fill_seed'] += 1
    changed = Baseline(exp,config)
    with pytest.raises(ConfigError,match='--force'):
        Steps(changed,build_plan(changed))('pool',lambda:None)


def test_policy_section_does_not_change_baseline_stage_hashes(baseline):
    _,config,_ = baseline
    without = deepcopy(config); del without['policies']
    for stage in ('prepare','scope','generate','screen','survive','cover','report'):
        assert stage_hash(config,stage) == stage_hash(without,stage)


@pytest.mark.parametrize('patch', [dict(threshold=0),dict(threshold=1.1),dict(concurrency={'dev_selection':0}),dict(set=[dict(name='../oops')]),dict(set=[dict(name='none')])])
def test_invalid_policy_configuration(baseline,patch):
    _,config,_ = baseline
    config['policies'].update(patch)
    with pytest.raises(ConfigError):validate(config)


def test_synthetic_commit_loop_imports_segment_one_and_deduplicates(baseline,monkeypatch):
    from test_survival.policy import execute, report
    exp,config,root = baseline
    b = Baseline(exp,config); p = plan(exp,config); step=Steps(b,p)
    new = test_test('replacement',version='fresh')
    new['origin_id'] = new['id']
    calls=[]
    def get(self,index,digest):
        calls.append((index,digest))
        return dict(methods=[dict(new,state='pass')])
    monkeypatch.setattr(execute.Runs,'get',get)
    execute.survive(b,p,{('dev-scope',2):[new]},step)
    for name in ['release-reset','half-fill']:
        rows=read_csv(exp/f'results/policy/{name}/survival.csv')
        assert [(r['index'],r['event'],r['dev-scope_alive']) for r in rows] == [('0','evaluate','1'),('1','evaluate','1'),('2','evaluate','0'),('2','regenerate','1'),('3','evaluate','1')]
        write_json(exp/f'results/policy/{name}/coverage-total.json',dict(executable=0,groups={'dev-scope':0}))
    report.run(b)
    assert (exp/'results/policy/none/survival.csv').read_bytes() == (root/'results/survival.csv').read_bytes()
    assert read_csv(exp/'results/policy/summary.csv')[1]['dev-scope_entered_after_t']=='1'


def test_shared_file_run_is_executed_once_and_skip_has_evidence(baseline,monkeypatch):
    from test_survival.policy.execute import Runs
    exp,config,_ = baseline
    b=Baseline(exp,config); steps=Steps(b,build_plan(b)); runs=Runs(b,steps)
    tests=[test_test('a'),test_test('b')]
    for t in tests:t['file_hash']='one-physical-source'
    runs.register(tests); runs.ready=True
    calls=[]
    monkeypatch.setattr(runs.adapter,'build',lambda *_:dict(build='ok'))
    def execute(index,group,methods):
        calls.append((index,group,methods))
        assert len({t['file'] for t in methods})==1
        return dict(methods=[dict(t,state='pass') for t in methods],files=[])
    monkeypatch.setattr(runs.adapter,'run_group',execute)
    first=runs.get(1,'one-physical-source')
    second=runs.get(1,'one-physical-source')
    assert first==second and len(calls)==1
    b.skipped[3]='untouched'
    runs.carry(3,'one-physical-source')
    assert read_json(exp/'results/policy/runs/one-physical-source/3.json')['status']=='skipped-untouched'
    assert len(calls)==1


def test_changed_line_coverage_uses_union_and_preserves_empty_denominators(baseline,monkeypatch):
    from test_survival.policy import cover
    exp,config,root=baseline
    b=Baseline(exp,config)
    commit=b.by_index[1]
    write_csv(root/'results/changed-lines.csv',[dict(**{k:commit[k] for k in ('index','sha','date','subject')},scope_executable=2)],['index','sha','date','subject','scope_executable'])
    write_json(root/'results/changes/1.json',dict(post_image={'src/main/java/A.java':[1,2]}))
    t=test_test('a')
    for policy,tests in [('none',[t]),('half-fill',[t]),('release-reset',[])]:
        write_json(exp/f'results/policy/{policy}/alive.json',{'1':{'dev-scope':tests}})
    monkeypatch.setattr(cover.JavaMavenAdapter,'build',lambda *_:dict(build='ok'))
    monkeypatch.setattr(cover.JavaMavenAdapter,'setup_runner',lambda *_:None)
    calls=[]
    def measure(adapter,index,tests,name):
        calls.append(tests)
        return dict(methods=[dict(t,lines=[['src/main/java/A.java',1]],state='pass')],universe=[['src/main/java/A.java',1],['src/main/java/A.java',2]])
    monkeypatch.setattr(cover,'coverage',measure)
    cover.run(b,Steps(b,build_plan(b)))
    assert len(calls)==1 and len(calls[0])==1
    empty=read_json(exp/'results/policy/release-reset/coverage-total.json')
    assert empty==dict(executable=2,groups={'dev-scope':0})
    assert read_json(exp/'results/policy/half-fill/coverage-total.json')['groups']['dev-scope']==1


def test_pool_and_baseline_records_import_without_java_execution(baseline,monkeypatch):
    from test_survival.policy.execute import Runs,baseline_tests
    exp,config,_=baseline
    b=Baseline(exp,config); runs=Runs(b,Steps(b,build_plan(b)))
    frozen=baseline_tests(b)
    runs.baseline_catalog=frozen
    tests=frozen['dev-scope'];runs.register(tests)
    monkeypatch.setattr(runs,'initialize',lambda:pytest.fail('Imported records must not launch Java'))
    imported=runs.get(1,tests[0]['file_hash'])
    assert imported['methods'][0]['state']=='pass' and imported['imported']
    new=test_test('new')
    runs.register([new]); runs.pool_catalog[2,new['file_hash']]={'A#new':dict(new,state='pass')}
    assert runs.get(2,new['file_hash'])['methods'][0]['state']=='pass'


def test_policy_runner_mounts_baseline_read_only(baseline):
    from test_survival.runner.docker import DockerRunner
    exp,config,root=baseline
    b=Baseline(exp,config)
    b.execution_config['runner']['docker'].update(image='fixture',mounts={str(root.parent):'/repo'})
    runner=DockerRunner(exp,b.execution_config)
    command=runner.command(['true'],exp)
    assert str(root)+':/repo/baseline:ro' in command
    assert str(root.parent)+':/repo' in command


def test_matching_target_uses_dev_segment_in_force(baseline):
    from test_survival.policy.pools import matching_target
    exp,config,_=baseline
    b=Baseline(exp,config)
    b.options['set']=[dict(name='fill',trigger='threshold',action='fill')]
    p={'points':[dict(group='dev-scope',trigger='threshold',index=2),dict(group='llm',trigger='threshold',index=3)]}
    pool=[test_test(str(i)) for i in range(3)]
    assert matching_target(b,p,'llm',3,{('dev-scope',2):pool})==3
    assert matching_target(b,p,'llm',3,{('dev-scope',2):pool,('dev-scope',3):pool[:2]})==2


def test_coverage_batches_by_resource_snapshot_not_test_method(tmp_path):
    from test_survival.policy.pools import coverage
    write_json(tmp_path/'results/builds/1.json',dict(sha='commit'))
    tests=[dict(test_test(str(i)),resource_root='snapshot-a' if i<4 else 'snapshot-b') for i in range(7)]
    jobs=[]
    def worker(job):
        jobs.append(job)
        return dict(methods=job['methods'],universe=[['A.java',1]])
    result=coverage(SimpleNamespace(exp=tmp_path,worker=worker),1,tests,'union')
    assert [len(j['methods']) for j in jobs]==[4,3]
    assert len(result['methods'])==7 and result['universe']==[('A.java',1)]
