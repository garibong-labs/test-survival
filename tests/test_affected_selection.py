from copy import deepcopy
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from test_survival.selection import Selector, compiler_pom, reason, carry, executed, counts
from test_survival.config import validate
from test_survival.io import read_json, write_json
from test_junit4 import java_environment


def observation():
    return dict(state='pass', lines=[['src/main/java/p/A.java',4]], last_executed_index=0, last_executed_sha='t',
                selection_action='executed',dependency_record=dict(version=1,selection='affected',unbounded=False,
                files=['src/main/java/p/A.java'],context=dict(known=True,entries=[['g','a','1','','jar']])))


@pytest.mark.parametrize('change,paths,compiler,expected', [
    ({},[],False,None),
    ({},['src/main/java/p/A.java'],False,'dependency changed'),
    ({},['src/main/java/p/B.java'],False,None),
    ({},['src/main/resources/x'],False,'production resource changed'),
    ({},['src/main/kotlin/A.kt'],False,'production resource changed'),
    ({},['src/test/resources/x'],False,None),
    ({},['pom.xml'],False,None),
    ({},[],True,'compiler POM changed'),
    ({'dependency_record':{}},[],False,'missing segment dependencies'),
    ({'dependency_record':{'version':1,'selection':'all'}},[],False,'missing segment dependencies'),
    ({'remeasured_state':'fail'},[],False,'disagreeing repair'),
])
def test_selection_triggers(change,paths,compiler,expected):
    row = {**observation(),**change}
    context = observation()['dependency_record']['context']
    assert reason(row,2,'i',context,paths,compiler) == expected


def test_unbounded_classpath_missing_lines_and_segment_boundaries():
    row = observation(); context = deepcopy(row['dependency_record']['context'])
    assert reason(row,1,'i',context,[],False) is None  # control at t is eligible
    assert reason(row,2,'i',context,[],False,start=1) == 'missing segment dependencies'
    row['dependency_record']['unbounded'] = True
    assert reason(row,2,'i',context,[],False) == 'unbounded dependencies'
    row['dependency_record']['unbounded'] = False
    context['entries'][0][2] = '2'
    assert reason(row,2,'i',context,[],False) == 'classpath changed'
    context['known'] = False
    assert reason(row,2,'i',context,[],False) == 'unknown classpath'
    row.pop('lines')
    assert reason(row,2,'i',context,[],False) == 'missing observation'


def test_carried_lines_and_direct_origin_do_not_advance():
    row = observation(); saved = deepcopy(row)
    first = carry(row); second = carry(first)
    assert first['lines'] == saved['lines']
    assert second['last_executed_index'] == second['carried_from_index'] == 0
    assert row == saved
    fresh = executed(second,4,'four',row['dependency_record']['context'])
    assert 'carried_from_index' not in fresh
    assert fresh['last_executed_index'] == 4
    assert counts([first,fresh,{'state':'dead'}]) == {'executed':1,'carried':1}


@pytest.mark.parametrize('fragment', [
    '<parent><version>2</version></parent>',
    '<properties><maven.compiler.release>21</maven.compiler.release></properties>',
    '<properties><java.version>21</java.version></properties>',
    '<properties><sourceCompatibility>17</sourceCompatibility></properties>',
    '<properties><target>17</target></properties>',
    '<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><configuration><source>17</source></configuration></plugin></plugins></build>',
    '<profiles><profile><properties><compilerVersion>2</compilerVersion></properties></profile></profiles>',
])
def test_conservative_compiler_pom_triggers(fragment):
    assert compiler_pom('<project>'+fragment+'</project>') != compiler_pom('<project/>')


def test_cosmetic_pom_and_unrelated_properties_do_not_trigger():
    a = '<project xmlns="maven"><name>old</name><properties><encoding>UTF-8</encoding></properties></project>'
    assert compiler_pom(a) == compiler_pom('<project><name>new</name></project>')
    assert compiler_pom('<project><parent><version> 1 </version></parent></project>') == compiler_pom('<project><parent><version>1</version></parent></project>')


def test_endpoint_diff_includes_rename_failed_build_and_repository_parent(tmp_path):
    def git(*args): return subprocess.check_output(['git','-C',str(tmp_path),*args],text=True).strip()
    git('init','-q'); git('config','user.name','Fixture'); git('config','user.email','fixture@example.test')
    def commit():
        git('add','.'); git('commit','-qm','fixture'); return git('rev-parse','HEAD')
    source = tmp_path/'module/src/main/java/p/A.java'; source.parent.mkdir(parents=True); source.write_text('class A {}')
    (tmp_path/'pom.xml').write_text('<project><parent><version>1</version></parent></project>'); before=commit()
    source.rename(source.with_name('B.java')); middle=commit()  # unevaluable in the caller
    (tmp_path/'README').write_text('untouched'); commit()
    (tmp_path/'pom.xml').write_text('<project><parent><version>2</version></parent></project>'); after=commit()
    selector=Selector(SimpleNamespace(config={'project':{'repo':str(tmp_path),'module':'module'}}))
    paths, changed = selector.interval(before,after)
    assert set(paths) == {'src/main/java/p/A.java','src/main/java/p/B.java'} and changed
    assert selector.interval(middle,after) == ([],True)


@pytest.mark.java_integration
def test_real_iterative_compilation_and_dependency_graph(tmp_path, java_environment):
    image,_ = java_environment
    root = Path(__file__).parents[1]
    script = r'''
from pathlib import Path
import subprocess,sys,json
from types import SimpleNamespace
sys.path.insert(0,'/package')
from adapters.snapshot_compile import compile_snapshot
from adapters.dependencies import Dependencies
exp=Path('/out'); helpers=Path('/package/adapters/java')
def execute(command,cwd,log,timeout):
 log.parent.mkdir(parents=True,exist_ok=True)
 with log.open('w') as f: r=subprocess.run(command,cwd=cwd,stdout=f,stderr=subprocess.STDOUT,timeout=timeout)
 return dict(exit_code=r.returncode,log=str(log.relative_to(exp)),timeout=False)
w=SimpleNamespace(EXP=exp,HELPERS=helpers,JVM={'javac':'256m'},TIMEOUTS={'compile':60},execute=execute)
root=exp/'tests';root.mkdir()
(root/'Test.java').write_text('@interface Test {}')
(root/'Good.java').write_text('class Good { @Test void works() {} }')
(root/'ReflectionHelper.java').write_text('class ReflectionHelper {}')
for i in range(105): (root/f'Broken{i}.java').write_text(f'class Broken{i} {{ @Test void works() {{ Missing.no(); }} }}')
before={p.name:p.read_bytes() for p in root.iterdir()}
classes,result=compile_snapshot(w,root,[],exp/'all','',exp)
assert len(result['removed']) == 105, result
assert len(result['attempts']) == 2
assert (classes/'ReflectionHelper.class').exists() and (classes/'Good.class').exists()
assert not list(classes.glob('Broken*.class'))
assert before == {p.name:p.read_bytes() for p in root.iterdir()}
classes,result=compile_snapshot(w,root,[root/'Good.java'],exp/'selected','',exp,selected_only=True)
assert len(result['attempts']) == 1 and result['source_count'] == 3,result
# Class references stop at production; source identifiers recover inlined constants.
main=exp/'src/main/java/p';main.mkdir(parents=True)
(main/'A.java').write_text('package p; public class A { public static int call() { return Indirect.call(); } public static class Nested {} }')
(main/'Indirect.java').write_text('package p; public class Indirect { public static int call() { return 1; } }')
(main/'Constant.java').write_text('package p; public class Constant { public static final int VALUE=1; }')
(main/'RuntimeOnly.java').write_text('package p; public class RuntimeOnly {}')
production=exp/'target/classes';production.mkdir(parents=True)
subprocess.run(['javac','-d',str(production),*[str(p) for p in main.glob('*.java')]],check=True)
tests=exp/'deps';tests.mkdir()
(tests/'Use.java').write_text('class Use { int test() { return Helper.call(); } static class Inner { p.A.Nested n; } }')
(tests/'Helper.java').write_text('class Helper { static int call() { return p.Constant.VALUE + p.A.call(); } }')
classes,result=compile_snapshot(w,tests,[],exp/'deps-compile',str(production),exp)
work=exp/'deps-work';work.mkdir()
deps=Dependencies(w,exp,work)
row=deps.record({'class':'Use','file':str(tests/'Use.java')},classes,tests,{'p/RuntimeOnly'})
assert row['files'] == ['src/main/java/p/A.java','src/main/java/p/Constant.java','src/main/java/p/RuntimeOnly.java'],row
assert not row['unbounded'],row
row=deps.record({'class':'Use','file':str(tests/'Use.java')},classes,tests,{'unmapped/Generated'})
assert row['unbounded']
print('iterative compile and dependency graph passed')
'''
    r=subprocess.run(['docker','run','--rm','--network','none','-v',str(root/'src/test_survival')+':/package:ro',
                      '-v',str(tmp_path)+':/out',image,'python3','-c',script],capture_output=True,text=True,timeout=180)
    assert r.returncode == 0, r.stdout+r.stderr


def test_context_uses_final_worker_classpath_and_runner_jars(tmp_path, monkeypatch):
    from test_survival.adapters.selection_context import collect
    import json
    folder = tmp_path/'project'; folder.mkdir()
    helpers = tmp_path/'helpers'; helpers.mkdir()
    manifest = [dict(filename='org.jacoco.cli-0.8.14-nodeps.jar',coordinate='org.jacoco:org.jacoco.cli:0.8.14',classifier='nodeps'),
                dict(filename='org.jacoco.agent-0.8.14-runtime.jar',coordinate='org.jacoco:org.jacoco.agent:0.8.14',classifier='runtime'),
                dict(filename='runner.jar',coordinate='org.junit:junit:5',classifier='')]
    (helpers/'classpath-artifacts.json').write_text(json.dumps(manifest))
    project='/cache/g/a/1/a-1-tests.jar'; shadowed='/cache/org/junit/junit/4/junit-4.jar'
    def resolve(*args,**kwargs):
        output = next(x.split('=',1)[1] for x in kwargs['options'] if x.startswith('-DoutputFile='))
        Path(output).write_text(f'   g:a:test-jar:tests:1:test:{project} (optional)\x1b[36m -- module g.a\x1b[m\n   org.junit:junit:jar:4:test:{shadowed}\n')
    w=SimpleNamespace(EXP=tmp_path,HELPERS=helpers,TIMEOUTS={'build':10},execute=None,write_json=write_json,
                      classpath=lambda _: (folder,':'.join([str(folder/'target/classes'),str(tmp_path/'work/libs/runner.jar'),project])),
                      jacoco_version=lambda _: '0.8.14',maven_dependencies=SimpleNamespace(maven=resolve))
    collect(w,dict(index=1,sha='one',work='work/context',output='result.json'))
    result=read_json(tmp_path/'result.json')
    assert result['known']
    assert result['entries'][:2] == [['org.junit','junit','5','','jar'],['g','a','1','tests','test-jar']]
    assert len(result['entries']) == 4


def test_same_snapshot_can_have_different_policy_carry_origins(tmp_path):
    from test_survival.policy.affected import Decisions
    from test_survival.policy.segments import key, segment
    from test_policy import test_test
    test=test_test('one'); s1=segment(2,1,[test],[]); s2=segment(3,3,[test],[])
    b=SimpleNamespace(exp=tmp_path,by_index={4:{'sha':'four'}})
    runs=SimpleNamespace(adapter=SimpleNamespace(config={'project':{'repo':str(tmp_path),'module':'.'}}),initialize=lambda:None)
    decisions=Decisions(b,runs)
    row={**test,**observation(), 'last_executed_index':1,'last_executed_sha':'one'}
    newer={**row,'last_executed_index':3,'last_executed_sha':'three','lines':[['src/main/java/p/A.java',8]]}
    decisions.history['first','g',2,key(test)]=row
    decisions.history['second','g',3,key(test)]=newer
    decisions.selector=SimpleNamespace(context=lambda *a:observation()['dependency_record']['context'],decision=lambda *a,**kw:None)
    decisions.physical=lambda *a:pytest.fail('Both logical consumers must carry')
    result=decisions.measure(4,[('first','g',s1,{key(test)}),('second','g',s2,{key(test)})])
    assert result['first','g'][0]['carried_from_index']==1
    assert result['second','g'][0]['carried_from_index']==3
    assert result['second','g'][0]['lines'] != result['first','g'][0]['lines']


def test_fill_boundary_forces_one_shared_execution(tmp_path):
    from test_survival.policy.affected import Decisions
    from test_survival.policy.segments import key, segment
    from test_policy import test_test
    test=test_test('one'); s=segment(2,4,[test],[])
    b=SimpleNamespace(exp=tmp_path,by_index={4:{'sha':'four'}})
    runs=SimpleNamespace(adapter=SimpleNamespace(config={'project':{'repo':str(tmp_path),'module':'.'}}),initialize=lambda:None)
    d=Decisions(b,runs); calls=[]
    d.selector=SimpleNamespace(context=lambda *a:observation()['dependency_record']['context'],decision=lambda *a,**kw:None)
    def physical(index,wanted,context):
        calls.append(set(wanted));return {k:executed({**t,**observation()},index,'four',context) for k,t in wanted.items()}
    d.physical=physical
    result=d.measure(4,[('one','g',s,{key(test)}),('two','g',s,{key(test)})],boundary=True)
    assert calls == [{key(test)}]
    assert all(rows[0]['selection_action']=='executed' and rows[0]['last_executed_index']==4 for rows in result.values())


def test_policy_resume_requires_logical_rows_and_direct_physical_origin(tmp_path):
    from test_survival.policy.affected import logical_path, records_complete
    from test_survival.policy.segments import segment, execution_id
    from test_policy import test_test
    test=test_test('one');s=segment(2,1,[test],[])
    b=SimpleNamespace(exp=tmp_path,options={'set':[{'name':'p'}]},
                      by_index={0:{'sha':'t'},1:{'sha':'one'},2:{'sha':'two'}},evaluable=lambda _:True)
    write_json(tmp_path/'results/policy/p/segments.json',{'g':[segment(1,0,[test],[]),s]})
    row=executed({**test,**observation()},1,'one',observation()['dependency_record']['context'])
    origin=tmp_path/f'results/policy/runs/{execution_id(test)}/1.json'
    write_json(origin,dict(sha='one',methods=[row]))
    write_json(logical_path(b,'p','g',2,1),dict(sha='one',methods=[row]))
    write_json(logical_path(b,'p','g',2,2),dict(sha='two',methods=[carry(row)]))
    before={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert records_complete(b)
    assert before=={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    changed={**row,'lines':[]};write_json(origin,dict(sha='one',methods=[changed]))
    assert not records_complete(b)
    write_json(origin,dict(sha='one',methods=[row]));logical_path(b,'p','g',2,2).unlink()
    assert not records_complete(b)


def test_baseline_control_carries_lines_and_resume_validates_origin(tmp_path, monkeypatch):
    from test_batch_baseline import fixture, setup_worker
    from test_survival.stages import batch, control, survive
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.io import resolve_path
    config=fixture(tmp_path);config['survival']['selection']='affected';config['runner']['workers']['survive']=3
    jobs=[];setup_worker(monkeypatch,jobs)
    worker=JavaMavenAdapter.worker
    def instrumented(self,job):
        raw=worker(self,job)
        for t in raw['methods']:
            t['state']='pass'
            t['dependency_record']=dict(version=1,selection='affected',unbounded=False,files=['src/main/java/A.java'])
        return raw
    monkeypatch.setattr(JavaMavenAdapter,'worker',instrumented)
    monkeypatch.setattr(Selector,'context',lambda *args:dict(known=True,entries=[]))
    monkeypatch.setattr(Selector,'interval',lambda *args:([],False))
    control.run(tmp_path,config);survive.run(tmp_path,config)
    assert all(not j['methods'] for j in jobs if j['index'] > 0)
    row=read_json(tmp_path/'results/raw/6-dev.json')['methods'][0]
    assert row['selection_action']=='carried' and row['carried_from_index']==0
    assert row['lines']==read_json(tmp_path/'results/control/0-dev.json')['methods'][0]['lines']
    assert batch.stage_complete(tmp_path,config,'survive')
    before=len(jobs);survive.run(tmp_path,config);assert len(jobs)==before
    raw=read_json(tmp_path/'results/baseline-batches/0.json');raw['methods'][0]['lines']=[]
    write_json(tmp_path/'results/baseline-batches/0.json',raw,compress=True)
    assert not batch.stage_complete(tmp_path,config,'survive')
    survive.run(tmp_path,config)
    repaired=read_json(tmp_path/'results/raw/6-dev.json')['methods'][0]
    assert repaired['lines']==[]
    assert batch.stage_complete(tmp_path,config,'survive')


@pytest.mark.java_integration
def test_broad_runtime_hits_parallel_chunks_and_memory(tmp_path,java_environment):
    image,libs=java_environment; root=Path(__file__).parents[1]
    script=r'''
from pathlib import Path
import json,subprocess,sys,threading
sys.path.insert(0,'/package')
from adapters import java_worker as w,policy_coverage
exp=Path('/out');w.EXP=exp;w.SHARED=exp;w.HELPERS=Path('/package/adapters/java')
w.JVM={'javac':'256m','jacoco':'256m','launcher':'256m'};w.TIMEOUTS={'compile':60,'test_class':60}
(exp/'work').mkdir();(exp/'work/libs').symlink_to('/libs')
main=exp/'src/main/java';(main/'p').mkdir(parents=True);(main/'outside').mkdir()
(main/'p/A.java').write_text('package p;\npublic class A {\n public static int call() { return Nested.value()+Sibling.value(); }\n static class Nested { static int value() { return 1; } }\n}\nclass Sibling { static int value() { return 0; } }\n')
(main/'p/Constant.java').write_text('package p; public class Constant { public static final int VALUE=2; }')
(main/'outside/Hidden.java').write_text('package outside; public class Hidden { public static int call() { return 3; } }')
classes=exp/'target/classes';classes.mkdir(parents=True)
subprocess.run(['javac','-d',str(classes),*[str(p) for p in main.rglob('*.java')]],check=True)
records=json.loads((w.HELPERS/'classpath-artifacts.json').read_text())
cp=':'.join([str(classes),*['/libs/'+n for n in w.runner_test_jars(records)]])
w.classpath=lambda _: (exp,cp);w.jacoco_version=lambda _: '0.8.14'
methods=[]
for i in range(4):
 source=exp/f'tests/{i}';source.mkdir(parents=True)
 (source/'Helper.java').write_text('class Helper { static int value() { return p.Constant.VALUE; } }')
 (source/'T.java').write_text('class T { @org.junit.jupiter.api.Test void works() throws Exception { org.junit.jupiter.api.Assertions.assertEquals(1,p.A.call()); org.junit.jupiter.api.Assertions.assertEquals(2,Helper.value()); org.junit.jupiter.api.Assertions.assertEquals(3,Class.forName("outside.Hidden").getMethod("call").invoke(null)); org.junit.jupiter.api.Assertions.assertEquals("'+str(i)+'",java.nio.file.Files.readString(java.nio.file.Path.of("src/test/resources/id"))); Thread.sleep(200); } }')
 resources=exp/f'resources/{i}/resources';resources.mkdir(parents=True);(resources/'id').write_text(str(i))
 methods.append(dict(id=str(i),file=f'tests/{i}/T.java',source_root=f'tests/{i}',resource_root=f'resources/{i}',file_hash=str(i),execution_hash=str(i),**{'class':'T'},method='works'))
execute=w.execute;lock=threading.Lock();active=peak=0
barrier=threading.Barrier(2)
def measured(command,cwd,log,timeout):
 global active,peak
 is_test='PerTestCoverage' in command and '--universe' not in command
 if is_test:
  with lock: active+=1;peak=max(peak,active)
  barrier.wait(timeout=30)
 try: return execute(command,cwd,log,timeout)
 finally:
  if is_test:
   with lock: active-=1
w.execute=measured;policy_coverage.MAX_SNAPSHOT_GROUPS=1
policy_coverage.run(w,dict(index=0,sha='t',selection='affected',chunk_workers=2,methods=methods,survival=True,scope_universe=True,
 scope_files=['src/main/java/p/A.java'],scope_classes=['p.A'],work='job',output='result.json'))
result=json.loads((exp/'result.json').read_text())
assert peak==2,peak
for t in result['methods']:
 assert t['state']=='pass',t
 assert not t['dependency_record']['unbounded'],t
 assert t['dependency_record']['files']==['src/main/java/outside/Hidden.java','src/main/java/p/A.java','src/main/java/p/Constant.java'],t
 assert all(f=='src/main/java/p/A.java' for f,n in t['lines']),t
 assert {3,4,6} <= {n for f,n in t['lines']},t
for run in result['runs']:
 assert run['memory']['peak_rss_bytes']>0 and run['memory']['heap_pool_peak_sum_bytes']>0,run
assert not list((exp/'job').rglob('*.exec'))
assert not list((exp/'job').rglob('*.xml'))
policy_coverage.run(w,dict(index=0,sha='t',selection='affected',chunk_workers=2,methods=methods,survival=True,
 scope_files=['src/main/java/p/A.java','src/main/java/p/Constant.java','src/main/java/outside/Hidden.java'],
 scope_classes=['p.A','p.Constant','outside.Hidden'],work='full',output='full.json'))
full=json.loads((exp/'full.json').read_text())
by_id={t['id']:t for t in full['methods']}
for t in result['methods']:
 assert t['state']==by_id[t['id']]['state']
 assert t['lines']==[line for line in by_id[t['id']]['lines'] if line[0]=='src/main/java/p/A.java']
'''
    r=subprocess.run(['docker','run','--rm','--network','none','-v',str(root/'src/test_survival')+':/package:ro',
                      '-v',str(Path(libs).resolve())+':/libs:ro','-v',str(tmp_path)+':/out',image,'python3','-c',script],
                      capture_output=True,text=True,timeout=240)
    assert r.returncode==0,r.stdout+r.stderr
