"""Resume and one-pass policy contracts; no Docker, Java, or model execution."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_policy import baseline, test_test
from test_survival.adapters import policy_coverage, java_worker
from test_survival.config import canonical_hash, load, stage_hash, validate
from test_survival.io import read_csv, read_json, write_csv, write_json
from test_survival.policy.baseline import Baseline
from test_survival.policy.config import active_plan, digest_options
from test_survival.policy.execute import Runs
from test_survival.policy.pipeline import Steps
from test_survival.policy.config import reusable_run
from test_survival.policy.plan import build_plan, run as plan
from test_survival.policy.segments import execution_id, key


@pytest.mark.parametrize('jdk,major,expected', [(17,61,'0.8.12'),(25,61,'0.8.14'),(22,61,'0.8.14'),(17,69,'0.8.14')])
def test_jacoco_uses_runner_jdk_even_with_release_17_classes(tmp_path, monkeypatch, jdk, major, expected):
    (tmp_path/'Main.class').write_bytes(b'\xca\xfe\xba\xbe\x00\x00'+major.to_bytes(2,'big'))
    monkeypatch.setattr(java_worker.subprocess, 'run', lambda *a,**kw:SimpleNamespace(stdout='',stderr=f'openjdk version "{jdk}.0.1"'))
    assert java_worker.jacoco_version(tmp_path) == expected


@pytest.mark.parametrize('scenario', ['success','compile-failure','jvm-failure','timeout','test-failure'])
def test_batch_worker_and_per_file_fallbacks(tmp_path, scenario):
    root = tmp_path/'pool/sources'; root.mkdir(parents=True)
    for name in ('Good','Bad','Helper','Rejected'):
        (root/f'{name}.java').write_text('???;' if name == 'Rejected' else f'class {name} {{}}')
    original = {p:p.read_bytes() for p in root.iterdir()}
    tests = [dict(test_test(n), file=f'pool/sources/{n}.java', source_root='pool/sources', **{'class':n}) for n in ('Good','Bad')]
    calls = []; fallback_calls = []
    def execute(command, cwd, log, timeout):
        calls.append(command); log.parent.mkdir(parents=True,exist_ok=True); log.write_text('')
        failed = False
        if command[0] == 'javac':
            classes = Path(command[command.index('-d')+1])
            for name in ('PerTestCoverage','SourceInventory','DependencySources','SnapshotCompiler'):
                (classes/(name+'.class')).write_bytes(b'fixture helper')
        elif 'SnapshotCompiler' in command or any(a.endswith('SnapshotCompiler.java') for a in command):
            import base64
            n = next(i for i,a in enumerate(command) if a == 'SnapshotCompiler' or a.endswith('SnapshotCompiler.java'))
            sources = [Path(line) for line in Path(command[n+1]).read_text().splitlines()]
            assert all('???' not in p.read_text() for p in sources)
            failed = scenario == 'compile-failure' and any(p.name == 'Bad.java' for p in sources)
            log.write_text('Bad compile' if failed else '')
            errors = [(str(p), 'Bad compile') for p in sources if failed and p.name == 'Bad.java']
            Path(command[n+2]).write_text('\n'.join('\t'.join(base64.b64encode(v.encode()).decode() for v in pair) for pair in errors))
        elif 'SourceInventory' in command or 'SourceInventory.java' in ' '.join(command):
            source = Path(command[next(i for i, a in enumerate(command) if a == 'SourceInventory' or str(a).endswith('SourceInventory.java'))+1])
            assert not (source/'Rejected.java').exists()
            log.write_text('\n'.join(f'C\t{p.stem}\tclass\ttrue\t{p.stem}\t{p}\n'+
                                      (f'M\t{p.stem}\tworks\tplain' if p.stem != 'Helper' else '') for p in source.glob('*.java')))
        elif 'PerTestCoverage' in command:
            failed = scenario in ('jvm-failure','timeout')
            if not failed:
                for line in Path(command[-1]).read_text().splitlines():
                    out, _, selectors, *_ = line.split('\t'); out=Path(out);out.mkdir(parents=True)
                    records=[]
                    for n,selector in enumerate(Path(selectors).read_text().splitlines()):
                        cls,method=selector.split('\t')
                        import base64
                        message = base64.b64encode(('java.lang.IllegalStateException: '+('x'*5000)).encode()).decode() if scenario=='test-failure' else ''
                        state = 'FAILED' if scenario=='test-failure' else 'SUCCESSFUL'
                        if scenario=='test-failure':
                            records.append(f'{n+100}\t{cls}\t{method}\tSUCCESSFUL\t')
                            (out/f'{n+100}.tsv').write_text('')
                        records.append(f'{n}\t{cls}\t{method}\t{state}\t{message}')
                        (out/f'{n}.tsv').write_text('src/main/java/A.java\t1\ttrue\nsrc/main/java/Other.java\t9\ttrue\n')
                    (out/'tests.tsv').write_text('\n'.join(records)+'\n')
        return dict(exit_code=int(failed), timeout=scenario=='timeout' and 'PerTestCoverage' in command, log=str(log.relative_to(tmp_path)))
    def run_file(index, group, filename, methods, part, **kw):
        fallback_calls.append((filename,kw))
        assert not (tmp_path/methods[0]['source_root']/'Rejected.java').exists()
        assert kw == dict(retry=False,environment_retry=False)
        return dict(methods=[dict(t,state='error',message='fixture') for t in methods])
    w=SimpleNamespace(EXP=tmp_path, HELPERS=tmp_path, JVM={'javac':'512m','jacoco':'1g'},
                      TIMEOUTS={'compile':30,'test_class':10}, classpath=lambda _: (tmp_path,''),
                      jacoco_version=lambda _: '0.8.14', execute=execute, run_file=run_file, write_json=write_json)
    job=dict(index=1,sha='sha',methods=tests,survival=True,work='work/job',output='result.json',
             excluded_files=['pool/sources/Rejected.java'],scope_files=['src/main/java/A.java'],scope_classes=['A'])
    policy_coverage.run(w,job)
    result=read_json(tmp_path/'result.json')
    assert len([c for c in calls if 'PerTestCoverage' in c]) == 1
    assert len([c for c in calls if c[0] == 'java' and ('SnapshotCompiler' in c or any(a.endswith('SnapshotCompiler.java') for a in c))]) == (2 if scenario == 'compile-failure' else 1)
    assert len([c for c in calls if c[0] == 'javac']) == 1
    assert not any((tmp_path/'work/job/helper'/(name+'.class')).exists()
                   for name in ('SourceInventory','DependencySources','SnapshotCompiler'))
    assert original == {p:p.read_bytes() for p in root.iterdir()}
    if scenario in ('jvm-failure','timeout'):
        assert result['coverage_status']=='missing' and len(fallback_calls)==2
        assert {t['state'] for t in result['methods']} == {'fail'}
        assert all('lines' not in t for t in result['methods'])
    else:
        assert result['coverage_status']=='complete' and not fallback_calls
        assert {t['class']:t['state'] for t in result['methods']} == ({'Good':'fail','Bad':'fail'} if scenario=='test-failure' else {'Good':'pass','Bad':'compile-fail' if scenario=='compile-failure' else 'pass'})
        assert all(t['lines']==[['src/main/java/A.java',1]] for t in result['methods'] if t['state']=='pass')
        assert result['universe']==[['src/main/java/A.java',1]]
        if scenario=='test-failure':
            assert all(t['message'].startswith('java.lang.IllegalStateException: ') and len(t['message'])==4000 for t in result['methods'])


@pytest.mark.parametrize('version,coverage_status', [(None,'missing'),(1,'missing'),(2,'missing'),(None,'complete'),(1,'complete')])
def test_old_runs_recompute_together_and_pool_bytes_are_untouched(baseline, monkeypatch, version, coverage_status):
    exp,config,_=baseline;b=Baseline(exp,config);p=build_plan(b);steps=Steps(b,p)
    pool=exp/'state/policy-pool-llm-2.json';write_json(pool,dict(status='completed',config_hash=steps.digest))
    pool_before=(pool.read_bytes(),pool.stat().st_mtime_ns)
    runs=Runs(b,steps);tests=[test_test('a'),test_test('b')];runs.register(tests);runs.ready=True
    for t in tests:
        path=runs.path(1,execution_id(t));write_json(path,dict(coverage_version=version,coverage_status=coverage_status,
            coverage_reason='batch JVM failed or timed out' if coverage_status=='missing' else '',
            methods=[dict(t,state='pass',**({'lines':[]} if coverage_status=='complete' else {}))]))
        write_json(exp/f'state/policy-run-{execution_id(t)}-1.json',dict(status='completed',config_hash=steps.digest))
    jobs=[]
    monkeypatch.setattr(runs.adapter,'build',lambda *_:dict(build='ok'))
    def worker(job):
        jobs.append(job)
        return dict(methods=[dict(t,state='pass',lines=[['src/main/java/A.java',1],['outside.java',7]]) for t in job['methods']])
    monkeypatch.setattr(runs.adapter,'worker',worker)
    digests={execution_id(t) for t in tests}
    runs.run_index(1,digests);runs.run_index(1,digests)
    assert len(jobs)==1 and len(jobs[0]['methods'])==2
    assert all(read_json(runs.path(1,h))['methods'][0]['lines']==[['src/main/java/A.java',1]] for h in digests)
    assert pool_before==(pool.read_bytes(),pool.stat().st_mtime_ns)


def test_old_developer_plan_and_llm_pool_skip_without_force(baseline,monkeypatch,capsys):
    from test_survival.policy import pools
    exp,config,_=baseline
    saved=plan(exp,config); saved['pools'].append(dict(group='llm',index=2))
    write_json(exp/'results/policy/plan.json',saved)
    b=Baseline(exp,config)
    write_json(exp/'state/policy-plan.json',dict(status='completed',config_hash=canonical_hash(dict(options=digest_options(b.options),plan=saved))))
    steps=Steps(b,saved)
    output=exp/'results/policy/pools/llm/2/pool.json';write_json(output,[])
    steps('pool-llm-2',lambda:None,outputs=[output])
    before={p:p.read_bytes() for p in (exp/'state').glob('policy-*.json')}
    config['policies']['regenerate_developer']=False;b=Baseline(exp,config)
    assert build_plan(Baseline(exp,config))['pools']==[]
    assert plan(exp,config)==saved
    assert Steps(b,saved).digest==steps.digest
    b.groups = [*b.groups, 'llm']
    monkeypatch.setattr(pools,'build_pool',lambda *a:pytest.fail('No completed or developer pools may run'))
    class Progress:
        def event(self,name,message,**kw):print(name,message)
    assert pools.run(b,saved,Steps(b,saved,Progress()))=={('llm',2):[]}
    assert before=={p:p.read_bytes() for p in before}
    text=capsys.readouterr().out
    assert 'policy-plan: already completed; skipped' in text and 'pool-llm-2 already completed; skipped' in text


def test_missing_coverage_is_reported_and_dev_uses_baseline(baseline):
    from test_survival.policy import cover
    exp,config,root=baseline;config['policies']['regenerate_developer']=False;b=Baseline(exp,config)
    b.groups.append('llm')
    row=dict(index=1,sha='sha',date='date',subject='change',scope_executable=2,**{'scope_dev-scope_covered':1})
    write_csv(root/'results/changed-lines.csv',[row],list(row))
    write_json(root/'results/changes/1.json',dict(post_image={'src/main/java/A.java':[1,2]}))
    t=test_test('a')
    write_json(exp/f'results/policy/runs/{execution_id(t)}/1.json',dict(coverage_status='missing',coverage_reason='batch JVM failed or timed out',methods=[dict(t,state='pass')]))
    for p in b.options['set']:
        output=exp/'results/policy'/p['name']
        write_json(output/'alive.json',{'1':{'dev-scope':[], 'llm':[t]}})
        write_json(output/'segments.json',{'dev-scope':[{'start':0}], 'llm':[{'start':0},{'start':1}]})
    cover.run(b,None)
    for p in b.options['set']:
        output=exp/'results/policy'/p['name']
        result=read_csv(output/'coverage-commits.csv')[0]
        assert result['llm_covered']=='' and result['dev-scope_covered']=='1'
        assert result['dev-scope_reference']=='baseline (no regeneration)'
        assert read_csv(output/'coverage-missing.csv')==[dict(index='1',sha='sha',group='llm',reason='batch JVM failed or timed out')]
        assert read_json(output/'coverage-total.json')['groups']=={'dev-scope':1}


def test_selection_only_preserves_completed_stage_hashes(tmp_path):
    raw=dict(name='test',anchor='t',project=dict(repo=str(tmp_path)),dev=dict(groups=[dict(name='dev'),dict(name='dev-scope',denominator='coverage-scoped',derive_from='dev')]))
    before=validate(raw);raw['dev']['groups'][0]['frozen']=False;after=validate(raw)
    for stage in ('prepare','scope','generate','screen'):
        assert stage_hash(before,stage)==stage_hash(after,stage)
    for stage in ('control','survive','cover'):
        assert stage_hash(before,stage)!=stage_hash(after,stage)


def test_dev_reference_curves_and_labels_ignore_saved_developer_pools(baseline,monkeypatch):
    from test_survival.policy import execute, report, cover
    exp,config,root=baseline
    saved=build_plan(Baseline(exp,config))
    config['policies']['regenerate_developer']=False;b=Baseline(exp,config)
    monkeypatch.setattr(execute.Runs,'initialize',lambda *_:pytest.fail('Reference curves never launch Java'))
    execute.survive(b,saved,{('dev-scope',2):[test_test('new')]},Steps(b,saved))
    cover.run(b,None);report.run(b)
    for p in b.options['set']:
        out=exp/'results/policy'/p['name']
        curves=read_csv(out/'survival.csv')
        assert [r['dev-scope_alive'] for r in curves]==[r['dev-scope_alive'] for r in b.curves]
        assert all(r['event']=='evaluate' for r in curves)
        assert 'dev-scope (baseline reference)' in (out/'table1-survival-by-event.md').read_text()
    assert 'dev-scope (baseline reference)' in (exp/'results/policy/summary.md').read_text()


def test_baseline_accepts_selection_only_parent_without_raw_records(baseline):
    import yaml
    exp,config,root=baseline
    source=root.parent/'baseline.yaml';settings=yaml.safe_load(source.read_text())
    settings['dev']['groups'].insert(0,dict(name='dev',denominator='project',frozen=False))
    settings['dev']['groups'][1]['derive_from']='dev';source.write_text(yaml.safe_dump(settings))
    frozen=read_json(root/'results/frozen-denominators.json');frozen['dev']=frozen['dev-scope']
    write_json(root/'results/frozen-denominators.json',frozen)
    assert Baseline(exp,config).raw(1,'dev-scope')['methods'][0]['state']=='pass'


def test_selection_only_parent_is_skipped_by_control_survive_cover_and_report(tmp_path,monkeypatch):
    from contextlib import nullcontext
    from test_survival.stages import control, survive, cover
    from test_survival.report import tables, consistency, figures
    from test_survival.dashboard.build import unified
    config=validate(dict(name='fixture',anchor='t',project=dict(repo=str(tmp_path)),
                         dev=dict(groups=[dict(name='dev',frozen=False),dict(name='dev-scope',denominator='coverage-scoped',derive_from='dev')])))
    a=dict(id='a',file='dev/src-test-t/java/T.java',**{'class':'T'},method='a',kind='plain')
    bad=dict(a,id='bad',method='bad')
    frozen={'dev':[a,bad],'dev-scope':[a]}
    write_json(tmp_path/'results/frozen-denominators.json',frozen)
    write_json(tmp_path/'results/denominators.json',{'dev':2,'dev-scope':1})
    write_json(tmp_path/'results/anchor.json',dict(sha='anchor',date='date'))
    write_json(tmp_path/'results/commit-paths.json',{'1':['src/main/java/A.java']})
    commit=dict(index=1,sha='sha',date='date',subject='change',**{'changes-src-main':'yes','changes-pom':'no'})
    write_csv(tmp_path/'results/commits.csv',[commit],list(commit))
    write_json(tmp_path/'results/builds/1.json',dict(build='ok'))
    write_json(tmp_path/'population/scope.json',dict(classes=[dict(file='src/main/java/A.java')]))
    calls=[]
    class Adapter:
        def __init__(self,*args):self.runner=self
        def setup_runner(self):pass
        def session(self,stage):return nullcontext()
        def run_group(self,index,group,methods,phase='A',*,dead_at=None):
            calls.append((phase,index,group));assert group=='dev-scope' and methods==[a]
            result=dict(index=index,group=group,status='evaluated',methods=[dict(a,state='pass',flaky=False)],files=[])
            write_json(tmp_path/f'results/raw/{index}-{group}.json',result);return result
        def git(self,*args):return 'parent' if args[0]=='rev-parse' else '--- a/src/main/java/A.java\n+++ b/src/main/java/A.java\n@@ -1 +1 @@\n-old\n+new'
        def coverage(self,index,files,methods,group):
            calls.append(('B',index,group));assert group=='dev-scope'
            write_csv(tmp_path/f'results/coverage/{index}-{group}-lines.csv',[dict(file='src/main/java/A.java',line=1,covered='yes')],['file','line','covered'])
            return dict(status='evaluated')
    for stage in (control,survive,cover):monkeypatch.setattr(stage,'JavaMavenAdapter',Adapter)
    control.run(tmp_path,config);survive.run(tmp_path,config);cover.run(tmp_path,config)
    assert read_json(tmp_path/'results/control-at-t.json').keys()=={'dev-scope'}
    assert 'dev_alive' not in read_csv(tmp_path/'results/survival.csv')[0]
    assert not (tmp_path/'results/raw-derived/1-dev-scope.json').exists()
    assert 'scope_dev_covered' not in read_csv(tmp_path/'results/changed-lines.csv')[0]
    assert read_json(tmp_path/'results/frozen-denominators.json')==frozen
    tables.build(tmp_path,config);figures.build(tmp_path,config)
    assert unified(tmp_path)['denominators']=={'dev-scope':1}
    summary=consistency.build(tmp_path,config)
    assert summary['denominators']=={'dev-scope':1}
    assert set(calls)=={('control',0,'dev-scope'),('A',1,'dev-scope'),('B',0,'dev-scope'),('B',1,'dev-scope')}


def test_screening_exclusions_keep_helpers_without_modifying_pool(tmp_path):
    from test_survival.policy.execute import screening_exclusions
    root=tmp_path/'results/policy/pools/ktester/10'
    for n,(name,methods,compile) in enumerate([('Kept',[{'state':'pass'}],{'exit_code':0}),
                                              ('Rejected',[],None),('Failed',[{'state':'fail'}],{'exit_code':0}),
                                              ('Helper',[],{'exit_code':0})]):
        path=root/f'generated/ktester/round1/test_classes/{name}.java';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('???;' if name=='Rejected' else f'class {name} {{}}')
        write_json(root/f'generated/ktester/round1/validation/{n}.json',dict(file=str(path.relative_to(root)),methods=methods,compile=compile))
    # KTester repair attempts under <test_classes>/temp/ are never helpers; an
    # unscreened sibling elsewhere (a package subdirectory) stays available.
    temp=root/'generated/ktester/round1/test_classes/temp/Kept_0.java';temp.parent.mkdir(parents=True)
    temp.write_text('class Kept_0 { String s = """x"""; }')
    sibling=root/'generated/ktester/round1/test_classes/util/Support.java';sibling.parent.mkdir(parents=True)
    sibling.write_text('class Support {}')
    source_root=str((root/'generated/ktester/round1/test_classes').relative_to(tmp_path))
    tests=[dict(file=str((root/'generated/ktester/round1/test_classes/Kept.java').relative_to(tmp_path)),source_root=source_root)]
    before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    excluded=screening_exclusions(tmp_path,{('ktester',10):tests},{},None,{'ktester':'ktester'})
    # 'Helper' is a candidate that compiled with no test method (KTester comments
    # failing tests out): excluded like any rejected candidate; `util/Support.java`
    # was never a candidate and stays.
    assert {Path(p).name for p in excluded}=={'Rejected.java','Failed.java','Helper.java','Kept_0.java'}
    assert before=={p:p.read_bytes() for p in before}
    summary=read_json(tmp_path/'results/policy/excluded-sources.json')
    assert summary=={'ktester/10':dict(kept=1,rejected=3,scratch=1)}


def test_legacy_imported_run_is_measured_again(baseline,monkeypatch):
    # Records the old importer copied from the baseline carry no executed lines,
    # so they cannot feed changed-line coverage; the method is executed instead.
    exp,config,_=baseline;b=Baseline(exp,config);steps=Steps(b,build_plan(b));runs=Runs(b,steps)
    t=test_test('a');runs.register([t]);path=runs.path(1,execution_id(t))
    write_json(path,dict(imported=True,coverage_version=3,methods=[dict(t,state='pass')]))
    state=exp/f'state/policy-run-{execution_id(t)}-1.json';write_json(state,dict(status='completed',config_hash=steps.digest))
    assert not reusable_run(read_json(path),required_methods={'A#a'})
    jobs=[]
    runs.ready=True
    monkeypatch.setattr(runs.adapter,'build',lambda *a: {'build':'ok'})
    def worker(job):
        jobs.append(job)
        return dict(methods=[dict(m,state='pass',lines=[['src/main/java/A.java',1]]) for m in job['methods']])
    monkeypatch.setattr(runs.adapter,'worker',worker)
    runs.run_index(1,{execution_id(t)},alive={key(t)},dead_at={})
    result=runs.get(1,execution_id(t))
    assert [m['id'] for m in jobs[0]['methods']]==['a'] and result['methods'][0]['lines']==[['src/main/java/A.java',1]]
    assert reusable_run(result,required_methods={'A#a'})


def test_survive_batches_all_policy_methods_once_per_index_and_resumes(baseline,monkeypatch):
    from test_survival.policy import execute
    exp,config,_=baseline;b=Baseline(exp,config);p=build_plan(b)
    for point in p['points']:
        point['index']=1
    for point in p['pools']:
        point['index']=1
    tests=[test_test('a'),test_test('b')]
    jobs=[]
    monkeypatch.setattr(execute.JavaMavenAdapter,'build',lambda *_:dict(build='ok'))
    monkeypatch.setattr(execute.JavaMavenAdapter,'setup_runner',lambda *_:None)
    def worker(self,job):
        jobs.append(job)
        return dict(coverage_status='complete',methods=[dict(t,state='pass',message='',lines=[]) for t in job['methods']])
    monkeypatch.setattr(execute.JavaMavenAdapter,'worker',worker)
    execute.survive(b,p,{('dev-scope',1):tests},Steps(b,p))
    # Index 1 is the regeneration commit: one batch for both policies' new
    # compositions (the reset pool and the method the fill policy carries over);
    # later indices batch all three. Prefetched indices run on several workers.
    assert sorted((j['index'],sorted(t['method'] for t in j['methods'])) for j in jobs)==[(1,['a','b','works']),(2,['a','b','works']),(3,['a','b','works'])]
    execute.survive(b,p,{('dev-scope',1):tests},Steps(b,p))
    assert len(jobs)==3


def test_screening_exclusions_cover_the_frozen_snapshot_a_fill_policy_executes(tmp_path):
    """A fill policy executes the methods it carries over from the baseline, whose
    snapshot copies every sibling of a kept test (KTester's temp/ attempts, rejected
    candidates); those are excluded like a pool's, from the baseline's own records."""
    from test_survival.policy.execute import screening_exclusions
    baseline=tmp_path/'baseline'; exp=tmp_path/'policy'
    relative='generated/ktester/round1/test_classes'
    for n,(name,methods,compile) in enumerate([('Kept',[{'state':'pass'}],{'exit_code':0}),('Rejected',[],None)]):
        write_json(baseline/f'generated/ktester/round1/validation/{n}.json',dict(file=f'{relative}/{name}.java',methods=methods,compile=compile))
    snapshot=exp/'results/policy/baseline-sources'/relative
    for name in ('Kept','Rejected','temp/Kept_0'):
        path=snapshot/f'{name}.java'; path.parent.mkdir(parents=True,exist_ok=True); path.write_text('???;' if 'Rejected' in name else 'class X {}')
    dev_root=exp/'results/policy/baseline-sources/dev/src-test-t/java'
    (dev_root/'temp').mkdir(parents=True); (dev_root/'temp/TempPackageTest.java').write_text('class TempPackageTest {}')  # a legitimate developer package
    frozen={'ktester':[dict(file=str((snapshot/'Kept.java').relative_to(exp)),source_root=str(snapshot.relative_to(exp)))],
            'dev-scope':[dict(file=str((dev_root/'ATest.java').relative_to(exp)),source_root=str(dev_root.relative_to(exp)))]}
    excluded=screening_exclusions(exp,{},frozen,baseline,{'ktester':'ktester','dev-scope':'dev'})
    assert {Path(p).name for p in excluded}=={'Rejected.java','Kept_0.java'}
    assert all(p.startswith('results/policy/baseline-sources/') for p in excluded)
    summary=read_json(exp/'results/policy/excluded-sources.json')
    assert summary=={'ktester/t':dict(kept=1,rejected=1,scratch=1)}


def test_regeneration_batch_keeps_the_rows_the_policies_evaluated_from(baseline,monkeypatch):
    """The regeneration commit is measured twice when a source of the old composition
    is also part of the new one. The second batch executes only members without a
    usable row, keeps the window's rows (even without lines: the policies evaluated
    the commit from them), and writes its own evidence files; the next run's window
    remeasures the lineless rows."""
    from test_survival.policy import execute
    exp,config,_=baseline;b=Baseline(exp,config)
    b.execution_config['runner']['workers']['survive']=1
    b.options['set']=[dict(name='fill',trigger='threshold',action='fill')]
    plan=dict(points=[dict(group='dev-scope',index=i,trigger='threshold') for i in (1,2)],pools=[dict(group='dev-scope',index=i) for i in (1,2)])
    carried=dict(test_test('a'),execution_hash='shared'); dying=dict(test_test('b'),execution_hash='shared'); new=dict(test_test('n'),execution_hash='pool2')
    monkeypatch.setattr(execute,'baseline_tests',lambda _: {'dev-scope':[]})
    monkeypatch.setattr(b,'raw',lambda *a: {'methods':[]})
    monkeypatch.setattr(execute.JavaMavenAdapter,'build',lambda *a: {'build':'ok'})
    monkeypatch.setattr(execute.JavaMavenAdapter,'setup_runner',lambda *a: None)
    jobs=[]
    def worker(self,job):
        jobs.append(job)
        if job['index']==2 and job['output'].endswith('/2.json') and not any(j['index']==2 for j in jobs[:-1]):
            # The window's coverage JVM dies at the regeneration commit, once.
            return dict(coverage_status='missing',coverage_reason='batch JVM failed or timed out',
                        methods=[dict(t,state='fail' if t['id']=='b' else 'pass',message='boom') for t in job['methods']])
        # The regeneration batch would answer pass for b: the kept row must still say fail.
        regenerate=job['output'].endswith('-regenerate.json')
        return dict(methods=[dict(t,state='pass' if (t['id']!='b' or regenerate) else 'fail',lines=[]) for t in job['methods']])
    monkeypatch.setattr(execute.JavaMavenAdapter,'worker',worker)
    pools={('dev-scope',1):[carried,dying],('dev-scope',2):[new]}
    execute.survive(b,plan,pools,Steps(b,plan))
    record=read_json(exp/'results/policy/runs/shared/2.json')
    assert {t['id']:(t['state'],'lines' in t) for t in record['methods']}=={'a':('pass',False),'b':('fail',False)}
    assert [(j['index'],sorted(t['id'] for t in j['methods']),j['output'],j['work']) for j in jobs if j['index']==2]==[
        (2,['a','b'],'results/policy/batches/2.json','work/policy-survive/2'),
        (2,['n'],'results/policy/batches/2-regenerate.json','work/policy-survive/2-regenerate')]
    assert read_csv(exp/'results/policy/fill/survival.csv')[-1]['dev-scope_alive']=='2'  # a and n
    count=len(jobs)
    execute.survive(b,plan,pools,Steps(b,plan))  # the window remeasures the lineless rows, nothing else runs
    assert [(j['index'],sorted(t['id'] for t in j['methods'])) for j in jobs[count:]]==[(2,['a','b'])]
    record=read_json(exp/'results/policy/runs/shared/2.json')
    assert all('lines' in t for t in record['methods'])
    count=len(jobs)
    execute.survive(b,plan,pools,Steps(b,plan))
    assert len(jobs)==count


def test_regeneration_batch_never_replaces_a_row_another_policy_consumed(baseline,monkeypatch):
    """Reviewer's case: policy A resets at 1 onto [a]; policy B fills at 2 onto [a, b]
    of the same source. The window measured a at 2 (fail, with lines) and A lost it
    there; the regeneration batch must execute b only, so a resume reproduces A's curve."""
    from test_survival.policy import execute
    exp,config,_=baseline;b=Baseline(exp,config)
    b.execution_config['runner']['workers']['survive']=1
    b.options['set']=[dict(name='A',trigger='release',action='reset'),dict(name='B',trigger='threshold',action='fill')]
    plan=dict(points=[dict(group='dev-scope',index=1,trigger='release'),dict(group='dev-scope',index=2,trigger='threshold')],
              pools=[dict(group='dev-scope',index=i) for i in (1,2)])
    a=dict(test_test('a'),execution_hash='shared'); b2=dict(test_test('b'),execution_hash='shared')
    monkeypatch.setattr(execute,'baseline_tests',lambda _: {'dev-scope':[]})
    monkeypatch.setattr(b,'raw',lambda *x: {'methods':[]})
    monkeypatch.setattr(execute.JavaMavenAdapter,'build',lambda *x: {'build':'ok'})
    monkeypatch.setattr(execute.JavaMavenAdapter,'setup_runner',lambda *x: None)
    jobs=[]
    def worker(self,job):
        jobs.append(job)
        window=job['output'].endswith('/2.json')
        return dict(methods=[dict(t,state='fail' if (t['id']=='a' and window) else 'pass',lines=[]) for t in job['methods']])
    monkeypatch.setattr(execute.JavaMavenAdapter,'worker',worker)
    pools={('dev-scope',1):[a],('dev-scope',2):[a,b2]}
    execute.survive(b,plan,pools,Steps(b,plan))
    assert [(j['index'],sorted(t['id'] for t in j['methods'])) for j in jobs if j['index']==2]==[(2,['a']),(2,['b'])]
    assert {t['id']:t['state'] for t in read_json(exp/'results/policy/runs/shared/2.json')['methods']}=={'a':'fail','b':'pass'}
    curve=lambda: [r['dev-scope_alive'] for r in read_csv(exp/'results/policy/A/survival.csv')]
    first=curve(); assert first[-1]=='0'
    count=len(jobs)
    execute.survive(b,plan,pools,Steps(b,plan))
    assert len(jobs)==count and curve()==first


def test_reusable_run_replaces_malformed_rows_instead_of_crashing():
    assert not reusable_run(dict(coverage_version=3,methods=[dict(id='x',state='pass',lines=[])]),required_methods={'A#x'})
    assert reusable_run(dict(coverage_version=3,methods=[dict(id='x',**{'class':'A'},method='x',state='pass',lines=[])]),required_methods={'A#x'})


@pytest.mark.parametrize('repair_state', ['absent', 'compile-fail'])
@pytest.mark.parametrize('already_seeded', [False, True])
def test_reset_repairs_unusable_batch_seed_without_changing_verdict_or_pools(baseline, monkeypatch, repair_state, already_seeded):
    from test_survival.policy import execute, cover
    exp, config, root = baseline
    config['policies']['set'] = [dict(name='reset', trigger='release', action='reset')]
    b = Baseline(exp, config); saved = build_plan(b); steps = Steps(b, saved)
    t = execute.baseline_tests(b)['dev-scope'][0]
    digest = execution_id(t)
    for index in (1, 2, 3):
        raw = b.raw(index, 'dev-scope')
        raw.update(execution='batch', coverage_version=3, sha=b.by_index[index]['sha'])
        raw['methods'][0].update(execution_hash=digest, lines=[['src/main/java/A.java',1]])
        if index == 2:
            raw['methods'][0].update(state='fail', message='original failure', flaky=False,
                                     lines=[], remeasured_state=repair_state)
        write_json(b.raw_path(index, 'dev-scope'), raw)
    change = dict(index=2, sha=b.by_index[2]['sha'], date='date', subject='change', scope_executable=1)
    write_csv(root/'results/changed-lines.csv', [change], list(change))
    write_json(root/'results/changes/2.json', {'post_image':{'src/main/java/A.java':[1]}})
    pool_path = exp/'results/policy/pools/dev-scope/2/pool.json'
    steps('pool-dev-scope-2', lambda: write_json(pool_path, [t]), outputs=[pool_path])
    pool_bytes = pool_path.read_bytes()
    pool_state = (exp/'state/policy-pool-dev-scope-2.json').read_bytes()
    source_bytes = {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    path = exp/f'results/policy/runs/{digest}/2.json'
    if already_seeded:
        # A record produced by the old seeder claimed completeness incorrectly.
        steps(f'run-{digest}-2', lambda: write_json(path, dict(index=2, sha=b.by_index[2]['sha'],
              status='evaluated', coverage_version=3, coverage_status='complete', measurement_source='batch baseline',
              methods=[dict(t, state='fail', message='original failure', flaky=False, lines=[], remeasured_state=repair_state)])), outputs=[path])
        write_json(exp/'state/policy-survive.json', dict(status='completed', config_hash=steps.digest,
                   revision=dict(coverage_version=3, regenerate_developer=True, survive=2, developer={'reset':'regenerate'})))
    monkeypatch.setattr(execute.JavaMavenAdapter, 'build', lambda *_: {'build':'ok'})
    monkeypatch.setattr(execute.JavaMavenAdapter, 'setup_runner', lambda *_: None)
    jobs = []; recovered = False
    def worker(self, job):
        jobs.append(job)
        return dict(methods=[dict(m, state='pass' if recovered else repair_state, message='retry',
                                 lines=[['src/main/java/A.java',1]] if recovered else []) for m in job['methods']])
    monkeypatch.setattr(execute.JavaMavenAdapter, 'worker', worker)
    output = exp/'results/policy/reset'
    def attempt():
        steps('survive', lambda: execute.survive(b, saved, {('dev-scope',2):[t]}, steps), outputs=[output/'survival.csv'])
        steps('cover', lambda: cover.run(b, steps), outputs=[output/'coverage-total.json'])
        steps('report', lambda: write_json(output/'report-check.json', read_json(output/'coverage-total.json')),
              outputs=[output/'report-check.json'])
    attempt()
    first_curve = (output/'survival.csv').read_bytes()
    first_failures = (output/'first-failures.csv').read_bytes()
    for expected_jobs in (1, 2):
        assert len(jobs) == expected_jobs
        assert read_json(path)['coverage_status'] == 'missing'
        assert not reusable_run(read_json(path), required_methods={f'{t["class"]}#{t["method"]}'})
        assert read_csv(output/'coverage-commits.csv')[0]['dev-scope_covered'] == ''
        assert read_csv(output/'coverage-missing.csv')
        if expected_jobs == 1:
            attempt()  # Repeated failed repair remains resumable at the aggregate step.
    recovered = True
    attempt()
    assert len(jobs) == 3 and all(j['index'] == 2 and len(j['methods']) == 1 for j in jobs)
    row = read_json(path)['methods'][0]
    assert (row['state'], row['message'], row['remeasured_state']) == ('fail', 'original failure', 'pass')
    assert row['lines'] == [['src/main/java/A.java',1]]
    assert read_csv(output/'coverage-commits.csv')[0]['dev-scope_covered'] == '1'
    assert read_csv(output/'coverage-missing.csv') == []
    assert read_json(output/'report-check.json')['groups']['dev-scope'] == 1
    assert (output/'survival.csv').read_bytes() == first_curve
    assert (output/'first-failures.csv').read_bytes() == first_failures
    attempt()
    assert len(jobs) == 3
    assert pool_path.read_bytes() == pool_bytes
    assert (exp/'state/policy-pool-dev-scope-2.json').read_bytes() == pool_state
    assert source_bytes == {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_policy_seed_repairs_only_unusable_members(baseline, monkeypatch):
    exp, config, _ = baseline; b = Baseline(exp, config)
    runs = Runs(b, Steps(b, build_plan(b))); runs.ready = True
    tests = [dict(test_test(name), execution_hash='shared') for name in ('a','b','c')]
    runs.register(tests)
    observations = [dict(tests[0], state='fail', message='fallback'),
                    dict(tests[1], state='fail', message='failed retry', lines=[], remeasured_state='absent'),
                    dict(tests[2], state='pass', message='', lines=[])]
    monkeypatch.setattr(b, 'raw', lambda *_: dict(execution='batch', coverage_version=3,
                        sha=b.by_index[1]['sha'], methods=observations))
    monkeypatch.setattr(runs.adapter, 'build', lambda *_: {'build':'ok'})
    jobs = []
    def worker(job):
        jobs.append(job)
        return dict(methods=[dict(t, state='pass', lines=[['src/main/java/A.java',1]]) for t in job['methods']])
    monkeypatch.setattr(runs.adapter, 'worker', worker)
    runs.run_index(1, {'shared'})
    assert [[t['method'] for t in job['methods']] for job in jobs] == [['a','b']]
    rows = runs.get(1, 'shared')['methods']
    assert [t['state'] for t in rows] == ['fail','fail','pass']
    assert [t['message'] for t in rows] == ['fallback','failed retry','']
    assert rows[2]['lines'] == [] and 'remeasured_state' not in rows[2]
    runs.run_index(1, {'shared'})
    assert len(jobs) == 1


def test_remeasuring_a_lineless_row_refreshes_lines_but_keeps_the_fallback_verdict(baseline,monkeypatch):
    """When the coverage JVM died, the per-file fallback kept the survival verdict and
    a run may have evaluated from it. The next run measures the member again for
    its lines only: a different outcome does not change the recorded verdict, so
    survival.csv is reproducible across resumes."""
    from test_survival.policy.execute import Runs
    exp,config,_=baseline;b=Baseline(exp,config);steps=Steps(b,build_plan(b));runs=Runs(b,steps)
    t=dict(test_test('a'),execution_hash='shared');runs.register([t]);runs.ready=True
    path=runs.path(2,'shared')
    write_json(path,dict(index=2,sha=b.by_index[2]['sha'],status='evaluated',coverage_version=3,coverage_status='missing',
                         coverage_reason='batch JVM failed or timed out',methods=[dict(t,state='fail',message='boom',flaky=False)]))
    write_json(exp/'state/policy-run-shared-2.json',dict(status='completed',config_hash=steps.digest))
    monkeypatch.setattr(runs.adapter,'build',lambda *a: {'build':'ok'})
    monkeypatch.setattr(runs.adapter,'worker',lambda job: dict(methods=[dict(m,state='pass',message='',lines=[['src/main/java/A.java',1]]) for m in job['methods']]))
    runs.missing[(2,key(t))]='batch JVM failed or timed out'  # as survive would hold it from the previous record
    runs.run_index(2,{'shared'},alive={key(t)},dead_at={})
    row=runs.get(2,'shared')['methods'][0]
    assert (row['state'],row['message'],row['lines'])==('fail','boom',[['src/main/java/A.java',1]])
    assert row['remeasured_state']=='pass'
    assert read_json(path)['coverage_status']=='complete' and (2,key(t)) not in runs.missing  # the entry is popped


def test_regeneration_rewrite_carries_the_window_coverage_reason(baseline,monkeypatch):
    from test_survival.policy.execute import Runs
    exp,config,_=baseline;b=Baseline(exp,config);steps=Steps(b,build_plan(b));runs=Runs(b,steps)
    old=dict(test_test('a'),execution_hash='shared'); new=dict(test_test('n'),execution_hash='shared');runs.register([old,new]);runs.ready=True
    path=runs.path(2,'shared')
    write_json(path,dict(index=2,sha=b.by_index[2]['sha'],status='evaluated',coverage_version=3,coverage_status='missing',
                         coverage_reason='batch JVM failed or timed out',
                         methods=[dict(old,state='pass',message='',flaky=False),dict(new,state='not-selected',message='',flaky=False,lines=[])]))
    write_json(exp/'state/policy-run-shared-2.json',dict(status='completed',config_hash=steps.digest))
    monkeypatch.setattr(runs.adapter,'build',lambda *a: {'build':'ok'})
    jobs=[]
    def worker(job):
        jobs.append(job); return dict(coverage_reason='',methods=[dict(m,state='pass',message='',lines=[]) for m in job['methods']])
    monkeypatch.setattr(runs.adapter,'worker',worker)
    runs.run_index(2,{'shared'},alive={key(old),key(new)},dead_at={},label='regenerate',evaluated={key(old)})
    assert [m['id'] for m in jobs[0]['methods']]==['n']
    record=read_json(path)
    assert record['coverage_status']=='missing' and record['coverage_reason']=='batch JVM failed or timed out'
    assert runs.missing[(2,key(old))]=='batch JVM failed or timed out' and (2,key(new)) not in runs.missing


def test_developer_pool_keeps_its_temp_package(tmp_path):
    """Scratch exclusion is a generator rule; a developer pool with a `temp` test
    package keeps it in both trees."""
    from test_survival.policy.execute import screening_exclusions
    root=tmp_path/'results/policy/pools/dev-scope/2'
    src=root/'dev/src-test-t/java'
    for name in ('ATest','temp/TempPackageTest','temp/TempHelper'):
        path=src/f'{name}.java'; path.parent.mkdir(parents=True,exist_ok=True); path.write_text('class X {}')
    pool=[dict(file=str((src/'ATest.java').relative_to(tmp_path)),source_root=str(src.relative_to(tmp_path)))]
    assert screening_exclusions(tmp_path,{('dev-scope',2):pool},{},None,{'dev-scope':'dev'})==set()
    assert read_json(tmp_path/'results/policy/excluded-sources.json')=={'dev-scope/2':dict(kept=1,rejected=0,scratch=0)}
    with pytest.raises(ValueError,match='methods'):
        screening_exclusions(tmp_path,{},{'ktester':pool},tmp_path/'baseline')


def test_remeasure_that_does_not_run_the_method_keeps_the_row_lineless(baseline,monkeypatch):
    from test_survival.policy.execute import Runs
    exp,config,_=baseline;b=Baseline(exp,config);steps=Steps(b,build_plan(b));runs=Runs(b,steps)
    t=dict(test_test('a'),execution_hash='shared');runs.register([t]);runs.ready=True
    path=runs.path(2,'shared')
    write_json(path,dict(index=2,sha=b.by_index[2]['sha'],status='evaluated',coverage_version=3,coverage_status='missing',
                         coverage_reason='batch JVM failed or timed out',methods=[dict(t,state='pass',message='',flaky=False)]))
    write_json(exp/'state/policy-run-shared-2.json',dict(status='completed',config_hash=steps.digest))
    monkeypatch.setattr(runs.adapter,'build',lambda *a: {'build':'ok'})
    monkeypatch.setattr(runs.adapter,'worker',lambda job: dict(coverage_reason='',methods=[dict(m,state='compile-fail',message='javac',lines=[]) for m in job['methods']]))
    runs.run_index(2,{'shared'},alive={key(t)},dead_at={})
    record=read_json(path); row=record['methods'][0]
    assert row['state']=='pass' and 'lines' not in row and row['remeasured_state']=='compile-fail'
    assert record['coverage_status']=='missing' and record['coverage_reason'].startswith('remeasure did not execute')
    assert runs.missing[(2,key(t))]==record['coverage_reason']  # listed by survive, under the record's reason
    assert not reusable_run(record,required_methods={'A#a'})  # measured again by the next run


@pytest.mark.parametrize('verdict,remeasure,expect_lines,expect_reason', [
    ('fail','compile-fail',True,'remeasure did not execute'),  # Empty retry lines are not usable coverage.
    ('fail','pass',True,''),                    # same outcome class is not required for a non-pass verdict
    ('pass','pass',True,''),                    # agrees: settled
    ('pass','fail',True,'remeasure disagreed'), # lines adopted, disagreement surfaced
    ('pass','compile-fail',False,'remeasure did not execute'),
])
def test_settle_rules_for_remeasured_rows(verdict,remeasure,expect_lines,expect_reason,baseline,monkeypatch):
    from test_survival.policy.execute import Runs
    exp,config,_=baseline;b=Baseline(exp,config);steps=Steps(b,build_plan(b));runs=Runs(b,steps)
    t=dict(test_test('a'),execution_hash='shared');runs.register([t]);runs.ready=True
    path=runs.path(2,'shared')
    write_json(path,dict(index=2,sha=b.by_index[2]['sha'],status='evaluated',coverage_version=3,coverage_status='missing',
                         coverage_reason='batch JVM failed or timed out',methods=[dict(t,state=verdict,message='v',flaky=False)]))
    write_json(exp/'state/policy-run-shared-2.json',dict(status='completed',config_hash=steps.digest))
    monkeypatch.setattr(runs.adapter,'build',lambda *a: {'build':'ok'})
    monkeypatch.setattr(runs.adapter,'worker',lambda job: dict(methods=[dict(m,state=remeasure,message='r',lines=[]) for m in job['methods']]))
    runs.run_index(2,{'shared'},alive={key(t)},dead_at={})
    record=read_json(path); row=record['methods'][0]
    assert row['state']==verdict and row['remeasured_state']==remeasure and ('lines' in row)==expect_lines
    assert record['coverage_reason'].startswith(expect_reason)
    usable = expect_lines and remeasure in ('pass', 'fail')
    assert reusable_run(record,required_methods={'A#a'})==usable
    assert ((2,key(t)) in runs.missing)==(not usable)


def test_window_rewrite_keeps_measured_rows_of_members_outside_its_demand(baseline,monkeypatch):
    """The regeneration batch measured the new entrants at the commit; a later
    window rewrite of the same record (one window row lineless) keeps those rows."""
    from test_survival.policy.execute import Runs
    exp,config,_=baseline;b=Baseline(exp,config);steps=Steps(b,build_plan(b));runs=Runs(b,steps)
    old=dict(test_test('a'),execution_hash='shared'); new=dict(test_test('n'),execution_hash='shared');runs.register([old,new]);runs.ready=True
    path=runs.path(2,'shared')
    write_json(path,dict(index=2,sha=b.by_index[2]['sha'],status='evaluated',coverage_version=3,coverage_status='missing',
                         coverage_reason='batch JVM failed or timed out',
                         methods=[dict(old,state='pass',message='',flaky=False),dict(new,state='pass',message='',flaky=False,lines=[['src/main/java/A.java',2]])]))
    write_json(exp/'state/policy-run-shared-2.json',dict(status='completed',config_hash=steps.digest))
    monkeypatch.setattr(runs.adapter,'build',lambda *a: {'build':'ok'})
    jobs=[]
    def worker(job):
        jobs.append(job); return dict(methods=[dict(m,state='pass',message='',lines=[['src/main/java/A.java',1]]) for m in job['methods']])
    monkeypatch.setattr(runs.adapter,'worker',worker)
    runs.run_index(2,{'shared'},alive={key(old)},dead_at={})  # the window needs `a` only
    assert [m['id'] for m in jobs[0]['methods']]==['a']
    rows={r['id']:r for r in read_json(path)['methods']}
    assert rows['n']['state']=='pass' and rows['n']['lines']==[['src/main/java/A.java',2]]  # kept, no marker
    assert rows['a']['lines']==[['src/main/java/A.java',1]]


def test_excluded_for_follows_the_worker_root_rule(baseline):
    from test_survival.policy.execute import Runs, source_root_of
    exp,config,_=baseline;b=Baseline(exp,config);runs=Runs(b,Steps(b,build_plan(b)))
    runs.excluded={'results/policy/pools/k/1/gen/temp/A_0.java','results/policy/baseline-sources/gen/temp/B_0.java','elsewhere/temp/C.java'}
    with_root=dict(file='results/policy/pools/k/1/gen/A.java',source_root='results/policy/pools/k/1/gen/../gen')  # non-normalised
    without=dict(file='results/policy/baseline-sources/gen/B.java')  # the worker falls back to the file's folder
    assert source_root_of(without)=='results/policy/baseline-sources/gen'
    assert runs.excluded_for([with_root,without])=={'results/policy/pools/k/1/gen/temp/A_0.java','results/policy/baseline-sources/gen/temp/B_0.java'}
    assert runs.excluded_for([])==set()


def test_cover_reads_a_non_executing_remeasure_as_unavailable_not_zero(baseline,monkeypatch):
    """A row settled by a compile-fail remeasure carries empty lines. A policy that
    revives the member at that commit reads it: cover must report the coverage as
    unavailable rather than zero."""
    from test_survival.policy import cover
    exp,config,root=baseline;b=Baseline(exp,config)
    t=dict(test_test('a'),execution_hash='shared')
    out=exp/'results/policy/revive'; out.mkdir(parents=True)
    write_json(out/'alive.json',{'1':{'dev-scope':[t]}})
    write_json(out/'segments.json',{'dev-scope':[dict(segment=1,start=0,tests=[]),dict(segment=2,start=1,tests=[t])]})
    write_json(exp/'results/policy/runs/shared/1.json',dict(index=1,coverage_version=3,coverage_status='complete',coverage_reason='',
               methods=[dict(t,state='fail',message='v',lines=[],remeasured_state='compile-fail')]))
    row=dict(index=1,sha=b.by_index[1]['sha'],date='date',subject='change',scope_executable=1)
    write_csv(root/'results/changed-lines.csv',[row],list(row))
    write_json(root/'results/changes/1.json',dict(post_image={'src/main/java/A.java':[1]}))
    b.options['set']=[dict(name='revive',trigger='release',action='reset')]
    cover.run(b,None)
    assert read_csv(out/'coverage-commits.csv')[0]['dev-scope_covered']==''
    assert read_csv(out/'coverage-missing.csv')[0]['reason'].startswith('remeasure did not execute the method')


def test_cover_keeps_every_survival_missing_entry(baseline):
    """Survival may list an index under any reason, including one without changed
    scope lines; cover keeps those entries and adds its own."""
    from test_survival.policy import cover
    exp,config,root=baseline;b=Baseline(exp,config)
    out=exp/'results/policy/reset'; out.mkdir(parents=True)
    write_json(out/'alive.json',{'1':{'dev-scope':[]},'2':{'dev-scope':[]}})
    write_json(out/'segments.json',{'dev-scope':[dict(segment=1,start=0,tests=[])]})
    kept=[dict(index='1',sha=b.by_index[1]['sha'],group='dev-scope',reason='remeasure did not execute 1 passing method(s): compile-fail')]
    write_csv(out/'coverage-missing.csv',kept,['index','sha','group','reason'])
    row=dict(index=2,sha=b.by_index[2]['sha'],date='date',subject='change',scope_executable=1)  # index 1 changes no scope line
    write_csv(root/'results/changed-lines.csv',[row],list(row))
    write_json(root/'results/changes/2.json',dict(post_image={'src/main/java/A.java':[1]}))
    b.options['set']=[dict(name='reset',trigger='release',action='reset')]
    cover.run(b,None)
    assert read_csv(out/'coverage-missing.csv')==kept


def test_rewrite_carries_a_kept_disagreement_into_the_reason(baseline,monkeypatch):
    from test_survival.policy.execute import Runs
    exp,config,_=baseline;b=Baseline(exp,config);steps=Steps(b,build_plan(b));runs=Runs(b,steps)
    a=dict(test_test('a'),execution_hash='shared'); n=dict(test_test('n'),execution_hash='shared');runs.register([a,n]);runs.ready=True
    path=runs.path(2,'shared')
    write_json(path,dict(index=2,sha=b.by_index[2]['sha'],status='evaluated',coverage_version=3,coverage_status='complete',
                         coverage_reason='remeasure disagreed with the recorded verdict for 1 method(s)',
                         methods=[dict(a,state='pass',message='v',flaky=False,lines=[],remeasured_state='fail'),dict(n,state='not-selected',message='',flaky=False,lines=[])]))
    write_json(exp/'state/policy-run-shared-2.json',dict(status='completed',config_hash=steps.digest))
    monkeypatch.setattr(runs.adapter,'build',lambda *x: {'build':'ok'})
    monkeypatch.setattr(runs.adapter,'worker',lambda job: dict(methods=[dict(m,state='pass',message='',lines=[]) for m in job['methods']]))
    runs.run_index(2,{'shared'},alive={key(a),key(n)},dead_at={},label='regenerate',evaluated={key(a)})  # n enters; a is kept
    record=read_json(path)
    assert record['coverage_status']=='complete' and 'disagreed' in record['coverage_reason']


@pytest.mark.parametrize('selection', ['all','affected'])
@pytest.mark.parametrize('missing_second', [False,True])
def test_policy_coverage_distinguishes_colliding_snapshot_methods(baseline, selection, missing_second):
    from test_survival.policy import cover
    from test_survival.policy.affected import logical_path
    exp,config,root=baseline
    config['policies']['regenerate_developer']=False
    b=Baseline(exp,config);b.groups.append('llm')
    b.execution_config['survival']['selection']=selection
    tests=[dict(test_test('same'),execution_hash=digest) for digest in ('snapshot-a','snapshot-b')]
    change=dict(index=1,sha='sha',date='date',subject='change',scope_executable=2,**{'scope_dev-scope_covered':1})
    write_csv(root/'results/changed-lines.csv',[change],list(change))
    write_json(root/'results/changes/1.json',dict(post_image={'src/main/java/A.java':[1,2]}))
    observations=[dict(t,state='pass',lines=[['src/main/java/A.java',n]],selection_action='carried' if n==2 else 'executed')
                  for n,t in enumerate(tests,1) if not (missing_second and n==2)]
    for row in observations:
        write_json(exp/f'results/policy/runs/{execution_id(row)}/1.json',dict(methods=[row]))
    for policy in b.options['set']:
        name=policy['name'];out=exp/'results/policy'/name
        write_json(out/'alive.json',{'1':{'dev-scope':[], 'llm':tests}})
        write_json(out/'segments.json',{'dev-scope':[{'start':0}], 'llm':[{'start':0},{'start':1,'segment':2}]})
        write_json(logical_path(b,name,'llm',2,1),dict(methods=list(reversed(observations))),compress=True)
    cover.run(b,None)
    for policy in b.options['set']:
        out=exp/'results/policy'/policy['name'];row=read_csv(out/'coverage-commits.csv')[0]
        assert row['llm_covered']==('' if missing_second else '2')
        assert row['llm_percent']==('' if missing_second else '100.0')
        assert bool(read_csv(out/'coverage-missing.csv')) == missing_second


def test_affected_coverage_reads_and_indexes_each_logical_record_once(baseline, monkeypatch):
    from collections import Counter
    from test_survival.policy import cover
    from test_survival.policy.affected import logical_path
    exp,config,root=baseline
    config['policies']['regenerate_developer']=False
    b=Baseline(exp,config);b.groups.append('llm');b.execution_config['survival']['selection']='affected'
    tests=[dict(test_test(str(n)),execution_hash='snapshot') for n in range(1000)]
    change=dict(index=1,sha='sha',date='date',subject='change',scope_executable=100,**{'scope_dev-scope_covered':0})
    write_csv(root/'results/changed-lines.csv',[change],list(change))
    write_json(root/'results/changes/1.json',dict(post_image={'src/main/java/A.java':list(range(1,101))}))
    observations=[dict(t,state='pass',lines=[['src/main/java/A.java',n] for n in range(1,101)]) for t in tests]
    paths=[]
    for policy in b.options['set']:
        name=policy['name'];out=exp/'results/policy'/name
        write_json(out/'alive.json',{'1':{'dev-scope':[], 'llm':tests}})
        write_json(out/'segments.json',{'dev-scope':[{'start':0}], 'llm':[{'start':0},{'start':1,'segment':2}]})
        path=logical_path(b,name,'llm',2,1);paths.append(path)
        write_json(path,dict(methods=observations),compress=True)
    reads=Counter();keys=Counter();read=cover.read_json;identify=cover.key
    def counted(path,*args,**kwargs):
        reads[path]+=1
        return read(path,*args,**kwargs)
    def keyed(row):
        keys['rows']+=1
        return identify(row)
    monkeypatch.setattr(cover,'read_json',counted);monkeypatch.setattr(cover,'key',keyed)
    cover.run(b,None)
    assert {path:reads[path] for path in paths}==dict.fromkeys(paths,1)
    assert keys['rows']==2*len(tests)*len(paths)
    assert not any('/policy/runs/' in str(path) for path in reads)
    for policy in b.options['set']:
        out=exp/'results/policy'/policy['name']
        assert read_csv(out/'coverage-commits.csv')[0]['llm_covered']=='100'
        assert not read_csv(out/'coverage-missing.csv')
