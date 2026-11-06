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
                selection_action='executed',dependency_record=dict(version=4,selection='affected',unbounded=False,
                files=['src/main/java/p/A.java'],context=dict(known=True,entries=[['g','a','1','','jar']])))


def test_carried_origin_is_read_once_and_revalidated_after_replacement(tmp_path, monkeypatch):
    from test_survival.stages import batch
    m = batch.Measurements.__new__(batch.Measurements)
    from collections import OrderedDict
    m.exp, m.digest, m.origins = tmp_path, 'input', OrderedDict()
    actual = dict(observation(), execution_hash='snapshot', **{'class':'Example'}, method='test')
    path = tmp_path/'results/baseline-batches/0.json'
    write_json(path, dict(input_sha256='input', methods=[actual]), compress=True)
    reads = []
    original = batch.read_json
    def read(*args, **kwargs):
        reads.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(batch, 'read_json', read)
    for _ in range(50): assert m.valid_origin(carry(actual))
    assert len(reads) == 1
    changed = {**actual, 'lines':[['src/main/java/p/A.java',5]]}
    write_json(path, dict(input_sha256='input', methods=[changed]), compress=True)
    assert not m.valid_origin(carry(actual))
    assert len(reads) == 2
    assert m.valid_origin(carry(changed))
    assert len(reads) == 2
    from test_survival.io import resolve_path
    resolve_path(path).unlink()
    assert not m.valid_origin(carry(changed))


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
    ({'dependency_record':{'version':1,'selection':'affected'}},[],False,'missing segment dependencies'),
    ({'dependency_record':{'version':2,'selection':'affected'}},[],False,'missing segment dependencies'),
    ({'dependency_record':{'version':3,'selection':'affected'}},[],False,'missing segment dependencies'),
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


def test_selection_progress_counts_both_actions_from_a_single_pass_iterator():
    # Policy progress flattens per-policy ledgers with a generator.
    ledgers = {
        'reset': [{'state':'pass', 'selection_action':'executed'},
                  {'state':'pass', 'selection_action':'carried'}],
        'fill': [{'state':'compile-fail', 'selection_action':'executed'},
                 {'state':'pass', 'selection_action':'carried'},
                 {'state':'dead', 'selection_action':'carried'},
                 {'state':'not-selected', 'selection_action':'executed'}],
    }
    result = counts(row for rows in ledgers.values() for row in rows)
    assert result == {'executed':2, 'carried':2}
    assert sum(result.values()) == 4


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


@pytest.mark.parametrize('state', ['pass','fail','compile-fail','absent'])
def test_policy_resume_replays_cached_evidence_without_remeasurement_markers(tmp_path, state):
    from test_survival.policy.affected import Decisions
    from test_survival.policy.segments import key, segment
    from test_survival.rules.coverage import usable_lines
    from test_policy import test_test
    test = test_test('one'); s = segment(2,1,[test],[])
    b = SimpleNamespace(exp=tmp_path,by_index={1:{'sha':'one'}})
    runs = SimpleNamespace(adapter=SimpleNamespace(config={'project':{'repo':str(tmp_path),'module':'.'}}),initialize=lambda:None)
    context = observation()['dependency_record']['context']
    row = executed({**test, **observation(), 'state':state, 'lines':[]},1,'one',context)
    consumers = [('reset','g',s,{key(test)})]
    def coordinator(measured):
        d = Decisions(b,runs)
        d.selector = SimpleNamespace(context=lambda *args:context)
        d.physical = lambda *args:{key(test):measured}
        return d
    first = coordinator(row).measure(1,consumers,boundary=True)
    resumed = coordinator(row).measure(1,consumers,boundary=True)
    assert resumed == first
    assert usable_lines(resumed['reset','g'][0])
    assert 'remeasured_state' not in resumed['reset','g'][0]
    if state == 'pass':
        changed = {**row, 'state':'compile-fail', 'remeasured_state':'compile-fail'}
        repaired = coordinator(changed).measure(1,consumers,boundary=True)['reset','g'][0]
        assert repaired['state'] == 'pass' and not usable_lines(repaired)


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
    outdated={**row,'dependency_record':{**row['dependency_record'],'version':3}}
    write_json(origin,dict(sha='one',methods=[outdated]))
    write_json(logical_path(b,'p','g',2,1),dict(sha='one',methods=[outdated]))
    write_json(logical_path(b,'p','g',2,2),dict(sha='two',methods=[carry(outdated)]))
    assert not records_complete(b)
    write_json(logical_path(b,'p','g',2,1),dict(sha='one',methods=[row]))
    write_json(logical_path(b,'p','g',2,2),dict(sha='two',methods=[carry(row)]))
    changed={**row,'lines':[]};write_json(origin,dict(sha='one',methods=[changed]))
    assert not records_complete(b)
    write_json(origin,dict(sha='one',methods=[row]));logical_path(b,'p','g',2,2).unlink()
    assert not records_complete(b)


@pytest.mark.parametrize('source', ['physical','baseline'])
def test_policy_execution_upgrades_outdated_dependency_evidence(tmp_path, source):
    from test_survival.policy.affected import Decisions
    from test_survival.policy.segments import key
    from test_policy import test_test
    test=test_test('one');context=observation()['dependency_record']['context']
    row=executed({**test,**observation()},1,'one',context)
    old={**row,'dependency_record':{**row['dependency_record'],'version':3}}
    b=SimpleNamespace(exp=tmp_path,by_index={1:{'sha':'one'}},groups=['g'],
                      scope={'classes':[{'file':'src/main/java/p/A.java','class':'p.A'}]},
                      raw=lambda *args:dict(sha='one',methods=[old] if source=='baseline' else []))
    calls=[]
    def worker(job):
        calls.append(job)
        return {'methods':[row]}
    adapter=SimpleNamespace(config={'project':{'repo':str(tmp_path),'module':'.'}},worker=worker)
    runs=SimpleNamespace(adapter=adapter,initialize=lambda:None,excluded_for=lambda tests:set(),needed=set(),
                         path=lambda index,digest:tmp_path/f'results/policy/runs/{digest}/{index}.json')
    if source=='physical':write_json(runs.path(1,test['file_hash']),dict(sha='one',methods=[old]))
    decisions=Decisions(b,runs)
    fresh=decisions.physical(1,{key(test):test},context)
    assert len(calls)==1 and fresh[key(test)]['dependency_record']['version']==4
    assert decisions.physical(1,{key(test):test},context)==fresh
    assert len(calls)==1


def test_outdated_policy_carry_does_not_preserve_an_unobserved_pass(tmp_path):
    from test_survival.policy.affected import Decisions, logical_path
    from test_survival.policy.segments import key, segment
    from test_policy import test_test
    test=test_test('one');context=observation()['dependency_record']['context']
    s=segment(2,1,[test],[]);b=SimpleNamespace(exp=tmp_path,by_index={2:{'sha':'two'}})
    old=carry(executed({**test,**observation()},1,'one',context))
    old['dependency_record']={**old['dependency_record'],'version':3}
    write_json(logical_path(b,'p','g',2,2),dict(sha='two',methods=[old]))
    runs=SimpleNamespace(adapter=SimpleNamespace(config={'project':{'repo':str(tmp_path),'module':'.'}}),initialize=lambda:None)
    decisions=Decisions(b,runs)
    decisions.selector=SimpleNamespace(context=lambda *args:context,decision=lambda *args,**kwargs:'missing segment dependencies')
    actual=executed({**test,**observation(),'state':'fail'},2,'two',context)
    decisions.physical=lambda *args:{key(test):actual}
    rows=decisions.measure(2,[('p','g',s,{key(test)})])['p','g']
    assert rows[0]['state']=='fail' and 'remeasured_state' not in rows[0]


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
            t['dependency_record']=dict(version=4,selection='affected',unbounded=False,files=['src/main/java/A.java'])
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
for i in range(6):
 source=exp/f'tests/{i}';source.mkdir(parents=True)
 (source/'Helper.java').write_text('class Helper { static int value() { return p.Constant.VALUE; } }')
 (source/'T.java').write_text('class T { @org.junit.jupiter.api.Test void works() throws Exception { org.junit.jupiter.api.Assertions.assertEquals(1,p.A.call()); org.junit.jupiter.api.Assertions.assertEquals(2,Helper.value()); org.junit.jupiter.api.Assertions.assertEquals(3,Class.forName("outside.Hidden").getMethod("call").invoke(null)); org.junit.jupiter.api.Assertions.assertEquals("'+str(i)+'",java.nio.file.Files.readString(java.nio.file.Path.of("src/test/resources/id"))); Thread.sleep(200); } }')
 if i>=4:
  # Vintage calls the reflected provider during discovery, before plan start.
  # No Hidden class identifier or bytecode reference can supply this dependency.
  (source/'T.java').write_text("""@org.junit.runner.RunWith(org.junit.runners.Parameterized.class) public class T {
 @org.junit.runners.Parameterized.Parameters public static Object[][] data() throws Exception { return new Object[][] {{Class.forName("outside.Hidden").getMethod("call").invoke(null)}}; }
 public T(Object value) { org.junit.Assert.assertEquals(3,value); }
 @org.junit.Test public void works() { org.junit.Assert.assertEquals(1,p.A.call()); org.junit.Assert.assertEquals(2,Helper.value()); }
}""")
 resources=exp/f'resources/{i}/resources';resources.mkdir(parents=True);(resources/'id').write_text(str(i))
 methods.append(dict(id=str(i),file=f'tests/{i}/T.java',source_root=f'tests/{i}',resource_root=f'resources/{i}',file_hash=str(i),execution_hash=str(i),**{'class':'T'},method='works'))
execute=w.execute;lock=threading.Lock();active=peak=0
barrier=threading.Barrier(2)
def measured(command,cwd,log,timeout):
 global active,peak
 is_test='PerTestCoverage' in command and '--universe' not in command
 if is_test:
  roots=next(arg.split('=',1)[1].split(':') for arg in command if arg.startswith('-Dsurvival.initializer.roots='))
  assert len(roots)==2,roots  # production plus this chunk's single snapshot
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


@pytest.mark.java_integration
def test_nested_shared_setup_dependencies_select_every_descendant(tmp_path, java_environment):
    image,libs=java_environment;root=Path(__file__).parents[1]
    script=r'''
from pathlib import Path
import json,subprocess,sys
sys.path.insert(0,'/package')
from adapters import java_worker as w,policy_coverage
from test_survival.selection import reason,executed
exp=Path('/out');w.EXP=w.SHARED=exp;w.HELPERS=Path('/package/adapters/java')
w.JVM={'javac':'256m','jacoco':'256m','launcher':'256m'};w.TIMEOUTS={'compile':60,'test_class':60}
(exp/'work/libs').mkdir(parents=True)
for jar in Path('/libs').glob('*.jar'):(exp/'work/libs'/jar.name).symlink_to(jar)
main=exp/'src/main/java';(main/'p').mkdir(parents=True);(main/'outside').mkdir()
(main/'p/A.java').write_text('package p; public class A { public static int value() { return 1; } }')
for name in ('Hidden','Middle','LeftOnly','Unrelated'):
 (main/f'outside/{name}.java').write_text('package outside; public class '+name+' { public static int value() { return 1; } }')
classes=exp/'target/classes';classes.mkdir(parents=True)
def compile_main():subprocess.run(['javac','-g','-d',str(classes),*[str(p) for p in main.rglob('*.java')]],check=True)
compile_main()
records=json.loads((w.HELPERS/'classpath-artifacts.json').read_text())
cp=':'.join([str(classes),*['/libs/'+n for n in w.runner_test_jars(records)]])
w.classpath=lambda _:(exp,cp);w.jacoco_version=lambda _:'0.8.14'
source=exp/'tests';source.mkdir()
(source/'Outer.java').write_text("""
import org.junit.jupiter.api.*;
import static org.junit.jupiter.api.Assertions.*;
class Outer {
 static int shared;
 @BeforeAll static void prepare() throws Exception { shared=(Integer)Class.forName("outside.Hidden").getMethod("value").invoke(null); }
 @Nested class Left { @Test void works() throws Exception { assertEquals(1,shared); assertEquals(1,p.A.value()); assertEquals(1,Class.forName("outside.LeftOnly").getMethod("value").invoke(null)); } }
 @Nested class Right {
  @org.junit.jupiter.params.ParameterizedTest @org.junit.jupiter.params.provider.ValueSource(ints={1,2})
  void works(int ignored) { assertEquals(1,shared); assertEquals(1,p.A.value()); }
 }
 @Nested @TestInstance(TestInstance.Lifecycle.PER_CLASS) class Group {
  int middle;
  @BeforeAll void prepare() throws Exception { middle=(Integer)Class.forName("outside.Middle").getMethod("value").invoke(null); }
  @Nested class First { @Test void works() { assertEquals(1,shared); assertEquals(1,middle); assertEquals(1,p.A.value()); } }
  @Nested class Second { @Test void works() { assertEquals(1,shared); assertEquals(1,middle); assertEquals(1,p.A.value()); } }
 }
}
class Separate {
 @Test void works() throws Exception { assertEquals(1,Class.forName("outside.Unrelated").getMethod("value").invoke(null)); assertEquals(1,p.A.value()); }
}
""")
methods=[dict(id=cls,file='tests/Outer.java',source_root='tests',resource_root='tests',file_hash='frozen',execution_hash='snapshot',
              **{'class':cls},method='works') for cls in ('Outer$Left','Outer$Right','Outer$Group$First','Outer$Group$Second','Separate')]
def run(label,mode,tests):
 policy_coverage.run(w,dict(index=0,sha=label,selection=mode,chunk_workers=1,methods=tests,survival=True,
  scope_files=['src/main/java/p/A.java'],scope_classes=['p.A'],work=label,output=label+'.json'))
 rows=json.loads((exp/(label+'.json')).read_text())['methods']
 return [{**next(t for t in tests if t['id']==row['id']), **row} for row in rows]
before=run('before','affected',methods)
for row in before:
 assert row['state']=='pass',row
 files=row['dependency_record']['files']
 assert not row['dependency_record']['unbounded'],row
 if row['class'].startswith('Outer$'):
  assert 'src/main/java/outside/Hidden.java' in files,(row['class'],files)
  if '$Group$' in row['class']:assert 'src/main/java/outside/Middle.java' in files,(row['class'],files)
  else:assert 'src/main/java/outside/Middle.java' not in files,(row['class'],files)
  assert ('src/main/java/outside/LeftOnly.java' in files)==(row['class']=='Outer$Left'),(row['class'],files)
 else:
  assert 'src/main/java/outside/Hidden.java' not in files,files
  assert 'src/main/java/outside/Middle.java' not in files,files
 assert all(file=='src/main/java/p/A.java' for file,line in row['lines'])
# Only a reflectively reached production file changes; frozen tests stay intact.
(main/'outside/Hidden.java').write_text('package outside; public class Hidden { public static int value() { return 2; } }')
compile_main();context=dict(known=True,entries=[])
selected=[row for row in before if reason(executed(row,0,'t',context),1,'changed',context,['src/main/java/outside/Hidden.java'],False)]
assert {row['class'] for row in selected}=={row['class'] for row in before if row['class'].startswith('Outer$')}
all_rows=run('changed-all','all',methods)
affected_rows=run('changed-affected','affected',selected)
carried={row['class']:row['state'] for row in before};carried.update({row['class']:row['state'] for row in affected_rows})
assert carried=={row['class']:row['state'] for row in all_rows}
assert all(row['state']=='fail' for row in affected_rows)
assert not list((exp/'before').rglob('*.exec')) and not list((exp/'before').rglob('*.xml'))
'''
    # The embedded script imports the normal package for the selection decision.
    r=subprocess.run(['docker','run','--rm','--network','none','-e','PYTHONPATH=/src',
                      '-v',str(root/'src')+':/src:ro','-v',str(root/'src/test_survival')+':/package:ro',
                      '-v',str(Path(libs).resolve())+':/libs:ro','-v',str(tmp_path)+':/out',image,'python3','-c',script],
                     capture_output=True,text=True,timeout=180)
    assert r.returncode==0,r.stdout+r.stderr


@pytest.mark.java_integration
@pytest.mark.parametrize('owner', ['test','production','failed','isolated','defined'])
def test_lazy_initializer_dependencies_survive_probe_resets(tmp_path, java_environment, owner):
    image,libs=java_environment;root=Path(__file__).parents[1]
    script=r'''
from pathlib import Path
import json,subprocess,sys
sys.path.insert(0,'/package')
from adapters import java_worker as w,policy_coverage
from test_survival.selection import reason,executed
exp=Path('/out');w.EXP=w.SHARED=exp;w.HELPERS=Path('/package/adapters/java')
w.JVM={'javac':'256m','jacoco':'256m','launcher':'256m'};w.TIMEOUTS={'compile':60,'test_class':60}
(exp/'work/libs').mkdir(parents=True)
for jar in Path('/libs').glob('*.jar'):(exp/'work/libs'/jar.name).symlink_to(jar)
main=exp/'src/main/java';(main/'p').mkdir(parents=True);(main/'outside').mkdir()
(main/'p/A.java').write_text('package p; public class A { public static int value() { return 1; } }')
(main/'outside/Hidden.java').write_text('package outside; public class Hidden { public static int value() { return 1; } }')
(main/'outside/AfterOnly.java').write_text('package outside; public class AfterOnly { public static int value() { return 1; } }')
source=exp/'tests';source.mkdir()
helper="""
public class Helper {
 public static final int value=load();
 static int load() { try { return (Integer)Class.forName("outside.Hidden").getMethod("value").invoke(null); } catch(Exception e) { throw new RuntimeException(e); } }
}
"""
owner=sys.argv[1]
if owner=='failed': helper=helper.replace('return (Integer)', 'int ignored=(Integer)').replace('.invoke(null);', '.invoke(null); throw new RuntimeException("initialization failed");')
helper_root=main/'outside' if owner in ('production','isolated','defined') else source
(helper_root/'Helper.java').write_text(('package outside;\n' if owner in ('production','isolated','defined') else '')+helper)
body='org.junit.jupiter.api.Assertions.assertEquals(1,'+('outside.' if owner in ('production','isolated','defined') else '')+'Helper.value);'
if owner=='isolated':body='try (java.net.URLClassLoader loader=new java.net.URLClassLoader(new java.net.URL[]{java.nio.file.Path.of("/out/target/classes").toUri().toURL()},ClassLoader.getPlatformClassLoader())) { org.junit.jupiter.api.Assertions.assertEquals(1,Class.forName("outside.Helper",true,loader).getField("value").get(null)); }'
if owner=='defined':body="""ClassLoader loader=new ClassLoader(ClassLoader.getPlatformClassLoader()) { protected Class<?> findClass(String name) throws ClassNotFoundException { try { byte[] data=java.nio.file.Files.readAllBytes(java.nio.file.Path.of("/out/target/classes/"+name.replace('.','/')+".class")); return defineClass(name,data,0,data.length); } catch(java.io.IOException e) { throw new ClassNotFoundException(name,e); } } }; org.junit.jupiter.api.Assertions.assertEquals(1,Class.forName("outside.Helper",true,loader).getField("value").get(null));"""
if owner=='failed':body='org.junit.jupiter.api.Assertions.assertThrows(LinkageError.class, () -> { int value=Helper.value; });'
(source/'T.java').write_text("""
@org.junit.jupiter.api.TestMethodOrder(org.junit.jupiter.api.MethodOrderer.OrderAnnotation.class)
class T {
 @org.junit.jupiter.api.Test @org.junit.jupiter.api.Order(1) void first() throws Exception { BODY Class.forName("outside.AfterOnly").getMethod("value").invoke(null); org.junit.jupiter.api.Assertions.assertEquals(1,p.A.value()); }
 @org.junit.jupiter.api.Test @org.junit.jupiter.api.Order(2) void second() throws Exception { BODY org.junit.jupiter.api.Assertions.assertEquals(1,p.A.value()); }
}
""".replace('BODY',body))
classes=exp/'target/classes';classes.mkdir(parents=True)
def compile_main():subprocess.run(['javac','-g','-d',str(classes),*[str(p) for p in main.rglob('*.java')]],check=True)
compile_main()
records=json.loads((w.HELPERS/'classpath-artifacts.json').read_text())
cp=':'.join([str(classes),*['/libs/'+n for n in w.runner_test_jars(records)]])
w.classpath=lambda _:(exp,cp);w.jacoco_version=lambda _:'0.8.14'
methods=[dict(id=name,file='tests/T.java',source_root='tests',resource_root='tests',file_hash='frozen',execution_hash='snapshot',
              **{'class':'T'},method=name) for name in ('first','second')]
if owner=='test':
 other=exp/'other';other.mkdir();(other/'Separate.java').write_text('class Separate { @org.junit.jupiter.api.Test void works() { org.junit.jupiter.api.Assertions.assertEquals(1,p.A.value()); } }')
 methods.append(dict(id='separate',file='other/Separate.java',source_root='other',resource_root='other',file_hash='separate',execution_hash='separate',**{'class':'Separate'},method='works'))
def run(label,mode,tests):
 policy_coverage.run(w,dict(index=0,sha=label,selection=mode,chunk_workers=1,methods=tests,survival=True,
  scope_files=['src/main/java/p/A.java']+(['src/main/java/outside/Helper.java'] if owner in ('production','isolated','defined') else []),
  scope_classes=['p.A']+(['outside.Helper'] if owner in ('production','isolated','defined') else []),work=label,output=label+'.json'))
 rows=json.loads((exp/(label+'.json')).read_text())['methods']
 return [{**next(t for t in tests if t['id']==row['id']), **row} for row in rows]
before=run('before','affected',methods)
for row in before:
 assert row['state']=='pass',row
 record=row['dependency_record']
 if row['id']=='separate':
  assert not record['unbounded'] and 'src/main/java/outside/Hidden.java' not in record['files'],row
  continue
 if row['method']=='second':assert 'src/main/java/outside/AfterOnly.java' not in record['files'],row
 if owner in ('failed','isolated','defined'):assert record['unbounded'],row
 else:
  assert not record['unbounded'],row
  assert 'src/main/java/outside/Hidden.java' in record['files'],row
(main/'outside/Hidden.java').write_text('package outside; public class Hidden { public static int value() { return 2; } }')
compile_main();context=dict(known=True,entries=[])
selected=[r for r in before if reason(executed(r,0,'t',context),1,'changed',context,['src/main/java/outside/Hidden.java'],False)]
assert len(selected)==2,selected
all_rows=run('changed-all','all',methods);affected_rows=run('changed-affected','affected',selected)
merged={r['id']:(r['state'],r['lines']) for r in before};merged.update({r['id']:(r['state'],r['lines']) for r in affected_rows})
assert merged=={r['id']:(r['state'],r['lines']) for r in all_rows}
assert all(r['state']==('pass' if owner=='failed' else 'fail') for r in affected_rows)
assert not list((exp/'before').rglob('*.exec')) and not list((exp/'before').rglob('*.xml'))
'''
    r=subprocess.run(['docker','run','--rm','--network','none','-e','PYTHONPATH=/src',
                      '-v',str(root/'src')+':/src:ro','-v',str(root/'src/test_survival')+':/package:ro',
                      '-v',str(Path(libs).resolve())+':/libs:ro','-v',str(tmp_path)+':/out',image,'python3','-c',script,owner],
                     capture_output=True,text=True,timeout=180)
    assert r.returncode==0,r.stdout+r.stderr


def test_selection_counts_normalize_legacy_outcomes_without_mutation():
    rows=[dict(state=state) for state in ('pass','fail','compile-fail','error','timeout','absent')]
    rows += [dict(state='pass',selection_action='executed'),dict(state='pass',selection_action='carried'),
             dict(state='dead',selection_action='executed'),dict(state='not-selected',selection_action='carried'),
             dict(state='build-fail'),dict(state='skipped-untouched')]
    before=deepcopy(rows)
    assert counts(iter(rows))==dict(executed=7,carried=1)
    assert rows==before
    assert all('dependency_record' not in row and 'last_executed_index' not in row for row in rows)
