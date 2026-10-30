"""Resume and one-pass policy contracts; no Docker, Java, or model execution."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from test_policy import baseline, test_test
from test_survival.adapters import policy_coverage, java_worker
from test_survival.config import canonical_hash, load, stage_hash, validate
from test_survival.io import read_csv, read_json, write_csv, write_json
from test_survival.policy.baseline import Baseline
from test_survival.policy.config import active_plan, digest_options
from test_survival.policy.execute import Runs
from test_survival.policy.pipeline import Steps
from test_survival.policy.plan import build_plan, run as plan
from test_survival.policy.segments import execution_id


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
        if command[0] == 'javac' and command[-1].startswith('@'):
            sources = [Path(line.strip('"')) for line in Path(command[-1][1:]).read_text().splitlines()]
            assert all('???' not in p.read_text() for p in sources)
            failed = scenario == 'compile-failure' and any(p.name == 'Bad.java' for p in sources)
            log.write_text('Bad compile' if failed else '')
        elif 'SourceInventory.java' in ' '.join(command):
            source = Path(command[next(i for i, a in enumerate(command) if str(a).endswith('SourceInventory.java'))+1])
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
             excluded_files=['pool/sources/Rejected.java'],scope_files=['src/main/java/A.java'])
    policy_coverage.run(w,job)
    result=read_json(tmp_path/'result.json')
    assert len([c for c in calls if 'PerTestCoverage' in c]) == 1
    assert len([c for c in calls if c[-1].endswith('whole-snapshot.args')]) == 1
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


@pytest.mark.parametrize('version', [None, 1])
def test_old_runs_recompute_together_and_pool_bytes_are_untouched(baseline, monkeypatch, version):
    exp,config,_=baseline;b=Baseline(exp,config);p=build_plan(b);steps=Steps(b,p)
    pool=exp/'state/policy-pool-llm-2.json';write_json(pool,dict(status='completed',config_hash=steps.digest))
    pool_before=(pool.read_bytes(),pool.stat().st_mtime_ns)
    runs=Runs(b,steps);tests=[test_test('a'),test_test('b')];runs.register(tests);runs.ready=True
    for t in tests:
        path=runs.path(1,execution_id(t));write_json(path,dict(coverage_version=version,methods=[dict(t,state='pass')]))
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


def test_prefetch_uses_configured_workers(baseline,monkeypatch):
    exp,config,_=baseline;b=Baseline(exp,config);b.execution_config['runner']['workers']['survive']=2
    runs=Runs(b,Steps(b,build_plan(b)));barrier=threading.Barrier(2);calls=[]
    def run(index,digests):
        calls.append(index);barrier.wait(timeout=5)
    monkeypatch.setattr(runs,'run_index',run)
    runs.prefetch([1,2],{'file'})
    assert sorted(calls)==[1,2]


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
        def run_group(self,index,group,methods,phase='A'):
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
    tests=[dict(file=str((root/'generated/ktester/round1/test_classes/Kept.java').relative_to(tmp_path)))]
    before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    excluded=screening_exclusions(tmp_path,{('ktester',10):tests})
    assert {Path(p).name for p in excluded}=={'Rejected.java','Failed.java'}
    assert before=={p:p.read_bytes() for p in before}


def test_legacy_imported_run_is_reused_byte_for_byte(baseline,monkeypatch):
    exp,config,_=baseline;b=Baseline(exp,config);steps=Steps(b,build_plan(b));runs=Runs(b,steps)
    t=test_test('a');runs.register([t]);path=runs.path(1,execution_id(t))
    write_json(path,dict(imported=True,methods=[dict(t,state='pass')]))
    state=exp/f'state/policy-run-{execution_id(t)}-1.json';write_json(state,dict(status='completed',config_hash=steps.digest))
    before={p:p.read_bytes() for p in (path,state)}
    monkeypatch.setattr(runs,'initialize',lambda:pytest.fail('Legacy imports must not run'))
    assert runs.get(1,execution_id(t))['methods'][0]['state']=='pass'
    assert before=={p:p.read_bytes() for p in before}


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
    assert sorted(j['index'] for j in jobs)==[2,3]
    assert all({t['method'] for t in j['methods']}=={'a','b'} for j in jobs)
    execute.survive(b,p,{('dev-scope',1):tests},Steps(b,p))
    assert len(jobs)==2
