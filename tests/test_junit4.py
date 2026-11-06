import json
import os
from pathlib import Path
import subprocess

import pytest

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.adapters.java_worker import parse_reports,runner_test_jars
from test_survival.config import validate
from test_survival.io import write_json


def test_auto_framework_uses_original_module_dependencies(tmp_path):
    config=validate({'name':'x','project':{'repo':str(tmp_path)},'anchor':'t'})
    adapter=JavaMavenAdapter(tmp_path,config)
    write_json(tmp_path/'results/builds/0.json',{'classpath':'cp.txt'})
    (tmp_path/'cp.txt').write_text('/root/.m2/repository/junit/junit/4.13.2/junit-4.13.2.jar')
    assert adapter.test_framework()=='junit4'
    with (tmp_path/'cp.txt').open('a') as f:f.write(':/junit-jupiter-engine-5.10.2.jar')
    assert adapter.test_framework()=='junit5'
    config['project']['test_framework']='junit4'
    assert adapter.test_framework()=='junit4'


@pytest.mark.parametrize('configured,expected', [('junit4', 'junit4'), ('junit5', 'junit5'), ('auto', 'auto')])
def test_setup_receives_project_framework_without_changing_generated_test_detection(tmp_path, monkeypatch, configured, expected):
    config = validate({'name': 'setup', 'anchor': 't', 'project': {'repo': str(tmp_path), 'test_framework': configured}})
    adapter = JavaMavenAdapter(tmp_path, config)
    adapter.folder(0).mkdir(parents=True)
    (adapter.folder(0)/'pom.xml').write_text('<project/>')
    jobs = []
    def execute(command, cwd, log, timeout):
        jobs.append(json.loads(Path(command[-1]).read_text()))
        if jobs[-1]['action'] == 'setup':
            jar_count = len(json.loads((adapter.helpers/'classpath-artifacts.json').read_text()))
            assert timeout >= jar_count*(4*config['runner']['timeouts']['build'] + 3*60)
        return {'exit_code': 0}
    monkeypatch.setattr(adapter.runner, 'execute', execute)
    adapter.setup_runner()
    assert jobs[-1]['action'] == 'setup'
    assert jobs[-1]['test_framework'] == expected
    assert not (tmp_path/'results/test-framework.json').exists()
    adapter.worker({'action': 'screen', 'group': 'llm'})
    assert jobs[-1]['test_framework'] == 'auto'


@pytest.fixture
def java_environment():
    image=os.environ.get('SURVIVAL_JAVA_TEST_IMAGE');libs=os.environ.get('SURVIVAL_JAVA_TEST_LIBS')
    if not image or not libs:
        message='Set SURVIVAL_JAVA_TEST_IMAGE and SURVIVAL_JAVA_TEST_LIBS for the container fixture'
        if os.environ.get('SURVIVAL_REQUIRE_JAVA_TESTS')=='1':pytest.fail(message)
        pytest.skip(message)
    if not Path(libs).is_dir() or not list(Path(libs).glob('*.jar')):
        pytest.fail('Fixture library directory is missing or empty')
    return image,libs


@pytest.mark.java_integration
def test_junit4_inventory_selectors_and_vintage_launcher(tmp_path,java_environment):
    image,libs=java_environment
    root=Path(__file__).parents[1]
    script=r'''
import pathlib,subprocess,json,sys
sources=sorted(str(p) for p in pathlib.Path('/fixture').glob('*.java'))
cp=':'.join('/libs/'+name for name in json.loads(sys.argv[1]))
pathlib.Path('/out/classes').mkdir()
subprocess.run(['javac','-cp',cp,'-d','/out/classes',*sources],check=True)
runtime='/out/classes:'+cp
inventory=subprocess.check_output(['java','/helpers/SourceInventory.java','/fixture','junit4'],text=True)
selectors=[]
for cls in ['fixture.PlainTest','fixture.ParameterizedTest']:
    result=subprocess.check_output(['java','-cp',runtime,'/helpers/MethodSelectors.java',cls,'junit4'],text=True)
    selectors.extend(line.split('\t')[1] for line in result.splitlines())
args=[arg for selector in selectors for arg in ['--select-method',selector]]
run=subprocess.run(['java','-cp',runtime,'org.junit.platform.console.ConsoleLauncher','--disable-banner','--include-engine','junit-vintage',*args,'--reports-dir','/out/reports'],capture_output=True,text=True)
print(json.dumps({'inventory':inventory,'selectors':selectors,'exit_code':run.returncode,'output':run.stdout+run.stderr}))
'''
    jars=runner_test_jars(json.loads((root/'src/test_survival/adapters/java/classpath-artifacts.json').read_text()))
    command=['docker','run','--rm','--network','none','-v',str(root/'tests/fixtures/junit4')+':/fixture:ro','-v',str(root/'src/test_survival/adapters/java')+':/helpers:ro','-v',str(Path(libs).resolve())+':/libs:ro','-v',str(tmp_path)+':/out',image,'python3','-c',script,json.dumps(jars)]
    result=json.loads(subprocess.check_output(command,text=True,timeout=120))
    assert result['exit_code']==0,result['output']
    tests=[line.split('\t')[1:4] for line in result['inventory'].splitlines() if line.startswith('M\t') and line.split('\t')[3]]
    assert tests==[['fixture.ParameterizedTest','works','parameterized'],['fixture.PlainTest','works','plain']]
    assert result['selectors']==['fixture.PlainTest#works','fixture.ParameterizedTest#works']
    observations=parse_reports(tmp_path/'reports')
    assert len(observations[('fixture.ParameterizedTest','works')])==2
    assert all(r['state']=='pass' for rows in observations.values() for r in rows)


@pytest.mark.java_integration
def test_real_worker_environment_retries_and_single_method_coverage(tmp_path,java_environment):
    import shutil
    from test_survival.io import read_json,read_csv
    image,libs=java_environment
    config=validate({'name':'fixture','project':{'repo':str(tmp_path),'test_framework':'junit4'},'anchor':'t',
                     'survival':{'environment_errors':{'patterns':['java.net.BindException'],'backoff_seconds':0}},
                     'runner':{'docker':{'image':image,'mounts':{str(tmp_path):'/experiment',str(Path(libs).resolve()):'/experiment/work/libs'}}}})
    source=tmp_path/'dev/src-test-t/java/fixture/EnvironmentProbe.java';source.parent.mkdir(parents=True)
    shutil.copy2(Path(__file__).parent/'fixtures/environment/EnvironmentProbe.java',source)
    production=tmp_path/'work/checkouts/0/src/main/java/fixture/Production.java';production.parent.mkdir(parents=True)
    production.write_text('package fixture; public class Production { public static int value() { return 2; } }')
    classes=tmp_path/'work/checkouts/0/target/classes';classes.mkdir(parents=True)
    (tmp_path/'work/runtime').mkdir(parents=True)
    (tmp_path/'work/classpath.txt').write_text('')
    write_json(tmp_path/'results/anchor.json',{'sha':'anchor'})
    write_json(tmp_path/'results/builds/0.json',{'sha':'anchor','build':'ok','classpath':'work/classpath.txt'})
    adapter=JavaMavenAdapter(tmp_path,config)
    result=adapter.runner.execute(['javac','-d',str(classes),str(production)],tmp_path,tmp_path/'production-compile.log',60)
    assert result['exit_code']==0
    row={'file':str(source.relative_to(tmp_path)),'class':'fixture.EnvironmentProbe','method':'transientBind','kind':'plain'}
    row['id']='|'.join(row[k] for k in ['file','class','method'])
    with adapter.runner.session('fixture'):
        raw=adapter.run_group(0,'dev',[row])
        method,=raw['methods']
        assert method['state']=='pass' and method['environment_reruns']==2 and not method['flaky']
        assert len(raw['files'][0]['runs'])==3
        (tmp_path/'work/runtime/environment-attempts.txt').unlink()
        covered=adapter.method_coverage(row)
        assert raw['whole_frozen_suite_compile']['cache']['status']=='miss'
        assert covered['whole_frozen_suite_compile']['cache']['status']=='hit'
        assert covered['methods'][0]['state']=='pass'
        assert covered['methods'][0]['environment_reruns']==2
        assert not covered['methods'][0]['flaky']
        coverage_base=tmp_path/'results/dev-method-coverage-t/fixture.EnvironmentProbe#transientBind'
        warm_lines=read_csv(coverage_base.with_name(coverage_base.name+'-lines.csv'))
        # Container-owned outputs must be reset by the same owner on Linux.
        reset=adapter.runner.execute(['python3','-c',
            'import pathlib, shutil; shutil.rmtree("work/compile-cache"); '
            'base="results/dev-method-coverage-t/fixture.EnvironmentProbe#transientBind"; '
            '[pathlib.Path(base+suffix).unlink() for suffix in (".json.gz", ".xml.gz")]; '
            'pathlib.Path("work/runtime/environment-attempts.txt").unlink()'],
            tmp_path,tmp_path/'fixture-cache-reset.log',30)
        assert reset['exit_code']==0
        cold=adapter.method_coverage(row)
        assert cold['whole_frozen_suite_compile']['cache']['status']=='miss'
        assert cold['methods']==covered['methods']
        assert read_csv(coverage_base.with_name(coverage_base.name+'-lines.csv'))==warm_lines
    assert (tmp_path/'results/dev-method-coverage-t/fixture.EnvironmentProbe#transientBind.xml.gz').exists()
    lines=read_csv(tmp_path/'results/dev-method-coverage-t/fixture.EnvironmentProbe#transientBind-lines.csv')
    assert any(r['file']=='src/main/java/fixture/Production.java' and r['covered']=='yes' for r in lines)


@pytest.mark.java_integration
@pytest.mark.parametrize('framework', ['jupiter', 'vintage'])
def test_policy_coverage_does_not_assign_container_teardown_to_next_test(tmp_path, java_environment, framework):
    image, libs = java_environment
    root = Path(__file__).parents[1]
    (tmp_path/'Production.java').write_text('''package fixture;
public class Production {
 public static int teardown() { return 1; }
 public static int body() { return 2; }
}''')
    sources = {
        'jupiter': '''package fixture;
import org.junit.jupiter.api.*;
@TestClassOrder(ClassOrderer.OrderAnnotation.class)
public class LifecycleTest {
 @Nested @Order(1) class A {
  @Test void first() {}
  @AfterAll static void cleanup() { Production.teardown(); }
 }
 @Nested @Order(2) class B {
  @org.junit.jupiter.params.ParameterizedTest
  @org.junit.jupiter.params.provider.ValueSource(ints={1,2})
  void second(int n) { Assertions.assertEquals(2, Production.body()); }
 }
}''',
        'vintage': '''package fixture;
import org.junit.*;
import org.junit.runner.RunWith;
import org.junit.runners.Suite;
@RunWith(Suite.class)
@Suite.SuiteClasses({LifecycleTest.A.class, LifecycleTest.B.class})
public class LifecycleTest {
 public static class A {
  @Test public void first() {}
  @AfterClass public static void cleanup() { Production.teardown(); }
 }
 public static class B {
  @Test public void second() { Assert.assertEquals(2, Production.body()); }
 }
}''',
    }
    (tmp_path/'LifecycleTest.java').write_text(sources[framework])
    script = r'''
from pathlib import Path
import json, subprocess, sys
cp=':'.join('/libs/'+p for p in json.loads(sys.argv[1]))
agent='/libs/org.jacoco.agent-0.8.14-runtime.jar'
cp+=':/libs/org.jacoco.cli-0.8.14-nodeps.jar:'+agent
def call(args): subprocess.run(args, check=True)
call(['javac','-d','/out/production','/out/Production.java'])
call(['javac','-cp',cp,'-d','/out/helper','/helpers/PerTestCoverage.java'])
call(['javac','-cp','/out/production:'+cp,'-d','/out/tests','/out/LifecycleTest.java'])
Path('/out/selectors.tsv').write_text('fixture.LifecycleTest\n')
runtime='/out/tests:/out/helper:/out/production:'+cp
Path('/out/batches.tsv').write_text('/out/dumps\t/out/production\t/out/selectors.tsv\t'+runtime+'\n')
call(['java','-javaagent:'+agent+'=output=none,dumponexit=false','-cp','/out/helper:'+cp,'PerTestCoverage','/out/batches.tsv'])
'''
    jars = runner_test_jars(json.loads((root/'src/test_survival/adapters/java/classpath-artifacts.json').read_text()))
    result = subprocess.run(['docker','run','--rm','--network','none',
                             '-v',str(root/'src/test_survival/adapters/java')+':/helpers:ro',
                             '-v',str(Path(libs).resolve())+':/libs:ro','-v',str(tmp_path)+':/out',
                             image,'python3','-c',script,json.dumps(jars)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout+result.stderr
    observations = [line.split('\t') for line in (tmp_path/'dumps/tests.tsv').read_text().splitlines()]
    assert [row[2] for row in observations] == (['first','second','second'] if framework == 'jupiter' else ['first','second'])
    for serial, cls, method, state, message in observations:
        assert state == 'SUCCESSFUL', message
        hits = {int(row.split('\t')[1]) for row in (tmp_path/f'dumps/{serial}.tsv').read_text().splitlines() if row.endswith('\ttrue')}
        assert hits == ({4} if method == 'second' else set()), 'container teardown leaked into test coverage'


@pytest.mark.java_integration
def test_policy_per_test_coverage_one_jvm_with_vintage_and_source_versions(tmp_path, java_environment):
    """Real listener/agent: parameterized cases, resets, and conflicting test versions."""
    image, libs = java_environment
    root = Path(__file__).parents[1]
    (tmp_path/'Production.java').write_text('''package fixture;
public class Production {
 public static int a() { return 1; }
 public static int b() { return 2; }
}''')
    for version, method in [('one','a'),('two','b')]:
        folder=tmp_path/version;folder.mkdir()
        (folder/'VersionTest.java').write_text('''package fixture;
import org.junit.jupiter.api.Test;
public class VersionTest {
 @Test void works() throws Exception {
  int result = (int) Class.forName("fixture.Provider").getMethod("value").invoke(null);
  org.junit.jupiter.api.Assertions.assertTrue(result > 0);
 }
}''')
        (folder/'Provider.java').write_text('package fixture; public class Provider { public static int value() { return Production.'+method+'(); } }')
    (tmp_path/'VintageTest.java').write_text('''package fixture;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.Parameterized;
@RunWith(Parameterized.class)
public class VintageTest {
 @Parameterized.Parameters public static Object[][] data() { return new Object[][] {{1},{2}}; }
 public VintageTest(int n) {}
 @Test public void works() { org.junit.Assert.assertEquals(1, Production.a()); }
}''')
    script = r'''
from pathlib import Path
import json,subprocess,sys
jars=json.loads(sys.argv[1]); cp=':'.join('/libs/'+p for p in jars)
cli='/libs/org.jacoco.cli-0.8.14-nodeps.jar'; agent='/libs/org.jacoco.agent-0.8.14-runtime.jar'
helpercp=cp+':'+cli+':'+agent
Path('/out/production').mkdir();Path('/out/helper').mkdir()
def call(args): subprocess.run(args,check=True,capture_output=True,text=True)
call(['javac','-d','/out/production','/out/Production.java'])
call(['javac','-cp',helpercp,'-d','/out/helper','/helpers/PerTestCoverage.java'])
manifest=[]
for version in ['one','two','vintage']:
 classes=Path('/out')/version/'classes';classes.mkdir(parents=True)
 source='/out/VintageTest.java' if version=='vintage' else '/out/'+version+'/VersionTest.java'
 sources=[source] if version=='vintage' else [str(p) for p in (Path('/out')/version).glob('*.java')]
 call(['javac','-cp','/out/production:'+cp,'-d',str(classes),*sources])
 selector=Path('/out')/(version+'.selectors');selector.write_text('fixture.VintageTest\n' if version=='vintage' else 'fixture.VersionTest\tworks\n')
 runtime=':'.join([str(classes),'/out/helper','/out/production',helpercp])
 manifest.append('\t'.join(['/out/'+version+'/dumps','/out/production',str(selector),runtime]))
Path('/out/batches.tsv').write_text('\n'.join(manifest)+'\n')
call(['java','-javaagent:'+agent+'=output=none,dumponexit=false','-cp','/out/helper:'+helpercp,'PerTestCoverage','/out/batches.tsv'])
# Exercise the real Python worker orchestration with the same compiled dependencies.
sys.path.insert(0,'/survival-package')
from adapters import java_worker as worker, policy_coverage
worker.EXP=Path('/out');worker.SHARED=worker.EXP;worker.HELPERS=Path('/helpers')
worker.JVM={'jacoco':'512m','javac':'256m','launcher':'512m'};worker.TIMEOUTS={'compile':60,'test_class':60}
Path('/out/work').mkdir();Path('/out/work/libs').symlink_to('/libs')
worker.classpath=lambda index:(Path('/out'),'/out/production:'+cp)
worker.jacoco_version=lambda classes:'0.8.14'
Path('/out/target').mkdir();Path('/out/target/classes').symlink_to('/out/production')
methods=[dict(id=version,file_hash='same-test-source',execution_hash=version,file=version+'/VersionTest.java',source_root=version,
              resource_root='resources',**{'class':'fixture.VersionTest'},method='works') for version in ['one','two']]
policy_coverage.run(worker,dict(index=0,sha='fixture',methods=methods[:1],full_suite=True,work='pool',output='pool-result.json'))
assert json.loads(Path('/out/pool-result.json').read_text())['methods'][0]['state']=='pass'
# An unrelated formerly-valid test must not hide runtime helpers for the alive test.
Path('/out/two/BrokenTest.java').write_text('package fixture; public class BrokenTest { @org.junit.jupiter.api.Test void removed() { Production.removed(); } }')
policy_coverage.run(worker,dict(index=0,sha='fixture',methods=methods,work='worker',output='worker-result.json'))
result=json.loads(Path('/out/worker-result.json').read_text())
assert len(result['methods'])==2 and all(t['state']=='pass' for t in result['methods']),result
assert {t['execution_hash'] for t in result['methods']}=={'one','two'}
assert result['compile'][0]['snapshot_full_suite']
assert not result['compile'][1]['snapshot_full_suite']
assert 'BrokenTest.java' in result['compile'][1]['removed']
assert 'Provider.java' in result['compile'][1]['attempts'][-1]['sources']
for number,line in [(0,3),(1,4)]:
 assert ['src/main/java/fixture/Production.java',line] in result['methods'][number]['lines']
worker.REPO=Path('/out');worker.DEVELOPER=False;worker.TEST_FRAMEWORK='auto';worker.FLAKY_RERUN=0
worker.ENVIRONMENT_ERRORS={'patterns':[],'max_reruns':0,'backoff_seconds':0}
for t in methods:
 record=worker.run_file(0,'policy',t['file'],[t],Path('/out')/('survive-'+t['id']),retry=False)
 assert record['methods'][0]['state']=='pass',record
# Different versions of the same class compile once PER ROOT without a
# duplicate-class failure. Rejected syntax never reaches helper inventory.
Path('/out/two/Rejected.java').write_text('???;')
policy_coverage.run(worker,dict(index=0,sha='fixture',methods=methods,survival=True,
    excluded_files=['two/Rejected.java','two/BrokenTest.java'],scope_files=['src/main/java/fixture/Production.java'],scope_classes=['fixture.Production'],
    work='combined',output='combined-result.json'))
combined=json.loads(Path('/out/combined-result.json').read_text())
assert combined['coverage_status']=='complete' and all(t['state']=='pass' for t in combined['methods']),combined
assert all(f=='src/main/java/fixture/Production.java' for t in combined['methods'] for f,n in t['lines'])
assert Path('/out/two/Rejected.java').read_text()=='???;'
assert all(c['snapshot_full_suite'] and c['exit_code']==0 for c in combined['compile'])
assert len({c['log'] for c in combined['compile']})==2
for number,line in [(0,3),(1,4)]:
 assert ['src/main/java/fixture/Production.java',line] in combined['methods'][number]['lines']
# Non-conflicting names must not leak runtime-only or compile-time dependencies.
for name in ('isolated-a','isolated-b','isolated-c'): (Path('/out')/name).mkdir()
Path('/out/isolated-a/ATest.java').write_text('package fixture; public class ATest { @org.junit.jupiter.api.Test public void works() throws Exception { Class.forName("fixture.ForeignHelper"); } }')
Path('/out/isolated-b/BTest.java').write_text('package fixture; public class BTest { @org.junit.jupiter.api.Test public void works() throws Exception { Class.forName("fixture.ForeignHelper"); } }')
Path('/out/isolated-b/ForeignHelper.java').write_text('package fixture; public class ForeignHelper {}')
Path('/out/isolated-c/CTest.java').write_text('package fixture; public class CTest { @org.junit.jupiter.api.Test public void works() { new ForeignHelper(); } }')
isolated=[dict(id=n,file_hash=n,execution_hash=n,file='isolated-'+n+'/'+n.upper()+'Test.java',source_root='isolated-'+n,
               **{'class':'fixture.'+n.upper()+'Test'},method='works') for n in ('a','b','c')]
policy_coverage.run(worker,dict(index=0,sha='fixture',methods=isolated,survival=True,
    scope_files=['src/main/java/fixture/Production.java'],scope_classes=['fixture.Production'],work='isolated',output='isolated-result.json'))
result=json.loads(Path('/out/isolated-result.json').read_text())
assert {t['id']:t['state'] for t in result['methods']}=={'a':'fail','b':'pass','c':'compile-fail'},result
assert next(t for t in result['methods'] if t['id']=='a')['message'].startswith('java.lang.ClassNotFoundException: fixture.ForeignHelper')
assert result['compile'][0]['snapshot_full_suite'] and result['compile'][1]['snapshot_full_suite']
policy_coverage.run(worker,dict(index=0,sha='fixture',methods=isolated[:1],survival=True,
    scope_files=['src/main/java/fixture/Production.java'],scope_classes=['fixture.Production'],work='alone',output='alone-result.json'))
assert json.loads(Path('/out/alone-result.json').read_text())['methods'][0]['state']=='fail'
# Direct scoped developer execution still compiles runtime-only frozen helpers.
import shutil
shutil.copytree('/out/one','/out/dev/src-test-t/java')
Path('/out/work/runtime').mkdir(exist_ok=True)
worker.DEVELOPER=True
selected=[dict(id='scoped',file='dev/src-test-t/java/VersionTest.java',**{'class':'fixture.VersionTest'},method='works',kind='plain')]
for phase in ('control','A','B'):
 worker.run_group(dict(index=0,sha='fixture',group='dev-scope',phase=phase,methods=selected,
                       input_sha256='fixture',runner_sha256='fixture',work='dev-'+phase,output='dev-'+phase+'.json'))
 result=json.loads((Path('/out')/('dev-'+phase+'.json')).read_text())
 assert result['methods'][0]['state']=='pass',result
 assert result['whole_frozen_suite_compile']['source_count']==2
'''
    jars=runner_test_jars(json.loads((root/'src/test_survival/adapters/java/classpath-artifacts.json').read_text()))
    result=subprocess.run(['docker','run','--rm','--network','none','-v',str(root/'src/test_survival/adapters/java')+':/helpers:ro',
                           '-v',str(Path(libs).resolve())+':/libs:ro','-v',str(root/'src/test_survival')+':/survival-package:ro','-v',str(tmp_path)+':/out',image,'python3','-c',script,json.dumps(jars)],capture_output=True,text=True,timeout=120)
    assert result.returncode == 0, result.stdout+result.stderr
    for version,count,line in [('one',1,3),('two',1,4),('vintage',2,3)]:
        folder=tmp_path/version/'dumps'
        observations=(folder/'tests.tsv').read_text().splitlines()
        assert len(observations)==count
        assert all(r.split('\t')[3]=='SUCCESSFUL' for r in observations)
        for row in observations:
            coverage=(folder/(row.split('\t')[0]+'.tsv')).read_text().splitlines()
            covered={int(r.split('\t')[1]) for r in coverage if r.endswith('\ttrue')}
            assert line in covered
            assert ({3,4}-{line}).isdisjoint(covered), 'execution data leaked between tests or source versions'


def test_direct_scoped_child_keeps_whole_frozen_tree_compilation(tmp_path,monkeypatch):
    config=validate(dict(name='fixture',anchor='t',project=dict(repo=str(tmp_path),test_framework='junit5'),
                         dev=dict(groups=[dict(name='dev',frozen=False),dict(name='dev-scope',derive_from='dev')])))
    adapter=JavaMavenAdapter(tmp_path,config)
    jobs=[]
    def execute(command,*args):
        jobs.append(json.loads(Path(command[-1]).read_text()))
        return dict(exit_code=0)
    monkeypatch.setattr(adapter.runner,'execute',execute)
    adapter.worker(dict(action='run',group='dev-scope'))
    assert jobs[0]['developer'] is True
    assert jobs[0].get('developer_suite',True) is True


@pytest.mark.java_integration
@pytest.mark.parametrize('mode', ['image', 'compose'])
def test_dependency_identity_with_real_runner(tmp_path, java_environment, mode, request):
    import hashlib
    from uuid import uuid4
    import yaml
    image, _ = java_environment
    docker = dict(image=image)
    if mode == 'compose':
        compose = tmp_path/'compose.yaml'
        project='identity-'+uuid4().hex
        (tmp_path/'Dockerfile').write_text('FROM '+image+'\n')
        compose.write_text(yaml.safe_dump(dict(name=project,
            services=dict(probe=dict(build='.', network_mode='none')))))
        # Give Compose its build-only image without creating new image layers.
        tag=project+'-probe'
        subprocess.run(['docker','tag',image,tag],check=True)
        request.addfinalizer(lambda:subprocess.run(['docker','image','rm',tag],check=True,stdout=subprocess.DEVNULL))
        docker = dict(compose_files=[str(compose)], service='probe')
    config = validate(dict(name='identity', anchor='t', project=dict(repo=str(tmp_path),module='module'),
                           runner=dict(docker=docker)))
    for exp in (tmp_path/'baseline', tmp_path/'baseline/results/policy/pools/dev/2'):
        adapter = JavaMavenAdapter(exp, config)
        adapter.folder(0).mkdir(parents=True)
        jar = exp/'dependency.jar'
        jar.write_bytes(b'fixture dependency')
        cp = exp/'work/classpath.txt'
        cp.write_text(adapter.runner.path(jar))
        directory = exp/'results/identity'
        assert adapter.cache.valid(cp, adapter.folder(0), 0, directory)
        removed=adapter.runner.execute(['python3','-c','import pathlib,sys; pathlib.Path(sys.argv[1]).unlink()',
                                        adapter.runner.path(jar)], adapter.folder(0), directory/'remove-jar.log', 30)
        assert removed['exit_code']==0
        assert not adapter.cache.valid(cp, adapter.folder(0), 0, directory)
        jar.write_bytes(b'fixture dependency')
        expected = [dict(path=adapter.runner.path(jar), sha256=hashlib.sha256(jar.read_bytes()).hexdigest())]
        assert adapter.dependency_identity(0, dict(classpath='work/classpath.txt'), directory) == expected
        assert json.loads((directory/'dependencies.json').read_text()) == expected
        assert (directory/'dependencies.log').is_file()
        (adapter.folder(0)/'pom.xml').write_text('<project><modelVersion>4.0.0</modelVersion><groupId>fixture</groupId><artifactId>module</artifactId><version>${revision}</version><packaging>pom</packaging><profiles><profile><id>marker</id><activation><file><exists>activate.marker</exists></file></activation></profile></profiles></project>')
        mvn=adapter.folder(0)/'.mvn';mvn.mkdir()
        (mvn/'maven.config').write_text('-Drevision=1\n')
        before=adapter.cache.context_identity(0)
        assert adapter.cache.context_identity(0)==before
        (adapter.folder(0)/'activate.marker').touch()
        assert adapter.cache.context_identity(0)!=before
        active=adapter.cache.context_identity(0)
        (mvn/'maven.config').write_text('-Drevision=2\n')
        assert adapter.cache.context_identity(0)!=active


@pytest.mark.java_integration
def test_container_owned_scratch_cleanup(tmp_path, java_environment, monkeypatch):
    import yaml
    from test_survival import work
    image,_=java_environment
    raw=dict(name='cleanup',anchor='t',project={'repo':str(tmp_path)},runner={'docker':{'image':image}})
    (tmp_path/'experiment.yaml').write_text(yaml.safe_dump(raw))
    adapter=JavaMavenAdapter(tmp_path,validate(raw))
    created=adapter.runner.execute(['python3','-c',
        'from pathlib import Path; p=Path("work/owned/nested/file"); p.parent.mkdir(parents=True); p.write_text("evidence")'],
        tmp_path,tmp_path/'create.log',30)
    assert created['exit_code']==0
    # Force the same Linux ownership boundary on Docker Desktop too. The
    # container imports the real implementation, not this host monkeypatch.
    monkeypatch.setattr(work,'_remove_owned',lambda *args: (_ for _ in ()).throw(PermissionError('container owner')))
    work.remove_owned(tmp_path,'work/owned')
    assert not (tmp_path/'work/owned').exists()
    records=list((tmp_path/'results/work-cleanup-commands').glob('*.json'))
    assert len(records)==1 and json.loads(records[0].read_text())['exit_code']==0
    # Real pools have no experiment.yaml, and may use a generator-specific
    # image. Exercise both checkout and successful scratch cleanup there.
    from test_survival.io import write_json
    pool=tmp_path/'results/policy/pools/dev/2'
    pool.mkdir(parents=True)
    config=validate(raw)
    runner=JavaMavenAdapter(pool,config).runner
    # Checkout/marker publication is host-owned before Maven creates its
    # root-owned target subtree, matching the production checkout path.
    write_json(pool/'work/checkouts/0.json',dict(sha='sha'))
    created=runner.execute(['python3','-c',
        'from pathlib import Path; '
        'paths=[Path("work/checkouts/0/target/classes/A.class"),Path("work/libs/runtime.jar")]; '
        '[(p.parent.mkdir(parents=True,exist_ok=True),p.write_bytes(b"compiled")) for p in paths]'],
        pool,pool/'create.log',30)
    assert created['exit_code']==0
    assert not (pool/'experiment.yaml').exists()
    write_json(pool/'results/builds/0.json',dict(sha='sha',build='ok'))
    write_json(pool/'results/workspace-identities/0.json',dict(sha='sha'))
    work.finish_checkouts(pool,config)
    work.finish_work(pool,config,pool=True)
    assert not (pool/'work/checkouts').exists()
    assert not (pool/'work/libs').exists()
    records=list((pool/'results/work-cleanup-commands').glob('*.json'))
    assert len(records)==4 and all(json.loads(p.read_text())['exit_code']==0 for p in records)


@pytest.mark.java_integration
@pytest.mark.parametrize('framework', ['junit4','junit5'])
def test_inherited_identity_survives_concrete_execution_and_batched_coverage(tmp_path, java_environment, framework):
    import hashlib
    from test_survival.rules.dev_denominator import frozen_method, identity
    image, libs = java_environment
    config=validate({'name':'inherited','project':{'repo':str(tmp_path),'test_framework':framework},'anchor':'t',
                     'dev':{'discovery':'build'},
                     'runner':{'docker':{'image':image,'mounts':{str(tmp_path):'/experiment',str(Path(libs).resolve()):'/experiment/work/libs'}}}})
    sources=tmp_path/'dev/src-test-t/java/fixture';sources.mkdir(parents=True)
    annotation='@org.junit.Test public' if framework=='junit4' else '@org.junit.jupiter.api.Test'
    (sources/'Base.java').write_text('package fixture; public abstract class Base { '+annotation+' void inherited() { if (Production.value()!=2) throw new AssertionError(); } '+annotation+' void excluded() { throw new AssertionError("non-candidate must not execute"); } }')
    parameterized='@org.junit.jupiter.params.ParameterizedTest @org.junit.jupiter.params.provider.ValueSource(strings={"value"})'
    if framework=='junit5':
        base=sources/'Base.java'
        base.write_text(base.read_text().rstrip().removesuffix('}')+parameterized+' public void works(java.lang.String value) { throw new AssertionError("override must execute"); } }')
        (sources/'GenericBase.java').write_text('package fixture; public abstract class GenericBase<T> { '+parameterized+' public void generic(T value) { throw new AssertionError("generic override must execute"); } }')
        (sources/'GenericMiddle.java').write_text('package fixture; public abstract class GenericMiddle<U> extends GenericBase<U> {}')
        (sources/'GenericChild.java').write_text('package fixture; public class GenericChild extends GenericMiddle<String> { '+parameterized+' @Override public void generic(String value) { if (Production.value()!=2) throw new AssertionError(); } }')
    expected_classes=set()
    for name in ('One','Two'):
        package='other' if framework=='junit5' and name=='Two' else 'fixture'
        directory=sources.parent/package;directory.mkdir(exist_ok=True)
        expected_classes.add(package+'.'+name)
        override = 'public void inherited() { if (Production.value()!=2) throw new AssertionError(); }' if framework=='junit4' and name=='One' else ''
        if framework=='junit5' and name=='One':
            override=parameterized+' @Override public void works(String value) { if (Production.value()!=2) throw new AssertionError(); }'
        (directory/(name+'.java')).write_text('package '+package+'; public class '+name+' extends fixture.Base { '+override+' }')
    production=tmp_path/'work/checkouts/0/src/main/java/fixture/Production.java';production.parent.mkdir(parents=True)
    production.write_text('package fixture; public class Production { public static int value() { return 2; } }')
    classes=tmp_path/'work/checkouts/0/target/classes';classes.mkdir(parents=True)
    (tmp_path/'work/runtime').mkdir(parents=True)
    (tmp_path/'work/classpath.txt').write_text('')
    write_json(tmp_path/'results/anchor.json',{'sha':'anchor'})
    write_json(tmp_path/'results/builds/0.json',{'sha':'anchor','build':'ok','classpath':'work/classpath.txt'})
    adapter=JavaMavenAdapter(tmp_path,config)
    with adapter.runner.session('inherited-fixture'):
        run=adapter.runner.execute(['javac','-d',str(classes),str(production)],tmp_path,tmp_path/'production-compile.log',60)
        assert run['exit_code']==0
        inventory=adapter.list_test_methods(sources.parent)
        if framework=='junit5':
            generic=[m for m in inventory['methods'] if m['class']=='fixture.GenericChild' and m['method']=='generic' and m['kind']]
            assert len(generic)==1
            assert inventory['classes']['fixture.GenericBase']['type_parameters']=='T'
            from test_survival.adapters.build_discovery import report_methods
            reports=tmp_path/'generic-reports';reports.mkdir()
            (reports/'TEST-child.xml').write_text('<testsuite><testcase classname="fixture.GenericChild" name="generic[1]"/></testsuite>')
            observed,unmapped,_=report_methods(reports,generic)
            assert observed=={('fixture.GenericChild','generic'):['pass']} and not unmapped
            member=frozen_method(identity(generic[0]))
            member.update(source_root='dev/src-test-t/java',resource_root='dev/src-test-t',
                          file_hash=hashlib.sha256((tmp_path/member['file']).read_bytes()).hexdigest())
            generic_run=adapter.run_group(0,'dev',[member])
            assert len(generic_run['methods'])==1 and generic_run['methods'][0]['state']=='pass',generic_run
            selections=[line for path in (tmp_path/'work/phase-A/0/dev').rglob('selectors-fixture.GenericChild.txt')
                        for line in path.read_text().splitlines()]
            assert selections==['generic\tfixture.GenericChild#generic(java.lang.String)']
            generic_coverage=adapter.worker(dict(action='policy-cover',index=0,sha='anchor',group='dev',methods=[member],
                candidate_suite=True,output='results/generic-candidate.json',work='work/generic-candidate'))
            assert len(generic_coverage['methods'])==1 and generic_coverage['methods'][0]['state']=='pass',generic_coverage
            assert generic_coverage['methods'][0]['lines']
            invocations=[line.split('\t')[2] for path in (tmp_path/'work/generic-candidate').rglob('tests.tsv')
                         for line in path.read_text().splitlines()]
            assert invocations==['generic']
            overridden=[m for m in inventory['methods'] if m['class']=='fixture.One' and m['method']=='works' and m['kind']]
            assert len(overridden)==1
            member=frozen_method(identity(overridden[0]))
            member.update(source_root='dev/src-test-t/java',resource_root='dev/src-test-t',
                          file_hash=hashlib.sha256((tmp_path/member['file']).read_bytes()).hexdigest())
            overridden_run=adapter.run_group(0,'dev',[member])
            assert len(overridden_run['methods'])==1 and overridden_run['methods'][0]['state']=='pass',overridden_run
        recovered=[m for m in inventory['methods'] if m.get('inherited') and m['method']=='inherited']
        assert {m['class'] for m in recovered}==expected_classes
        members=[]
        for row in recovered:
            row['declaring_file']=row['declaring_file'].replace('/experiment/','')
            t=frozen_method(identity(row))
            t.update(source_root='dev/src-test-t/java',resource_root='dev/src-test-t',
                     file_hash=hashlib.sha256((tmp_path/t['file']).read_bytes()).hexdigest())
            members.append(t)
        raw=adapter.run_group(0,'dev',members)
        assert all(m['state']=='pass' for m in raw['methods']),raw
        assert all(m['declaring_file'].endswith('/Base.java') for m in raw['methods'])
        covered=adapter.worker(dict(action='policy-cover',index=0,sha='anchor',group='dev',methods=members,
                                    candidate_suite=True,output='results/candidate.json',work='work/candidate'))
        assert len(covered['runs'])==1
        assert {(m['class'],m['state']) for m in covered['methods']}=={(cls,'pass') for cls in expected_classes}
        assert all(m['lines'] for m in covered['methods'])
        executed=[line.split('\t')[2] for path in (tmp_path/'work/candidate').glob('*/dumps/tests.tsv') for line in path.read_text().splitlines()]
        assert executed==['inherited','inherited']


@pytest.mark.java_integration
def test_candidate_coverage_preserves_isolated_setup_and_initializer_lines(tmp_path,java_environment):
    import hashlib
    from test_survival.rules.dev_denominator import frozen_method
    from test_survival.io import read_csv
    from test_survival.stages.scope import candidate_coverage
    image,libs=java_environment
    config=validate(dict(name='setup',anchor='t',project=dict(repo=str(tmp_path),test_framework='junit5'),
        dev=dict(discovery='source',unmapped='exclude',denominator='coverage-scoped'),
        runner=dict(docker=dict(image=image,mounts={str(tmp_path):'/experiment',str(Path(libs).resolve()):'/experiment/work/libs'}))))
    source=tmp_path/'dev/src-test-t/java/fixture/Shared.java';source.parent.mkdir(parents=True)
    source.write_text('''package fixture;
import org.junit.jupiter.api.*;
public class Shared {
 static int initialized=Production.initialized();
 static int prepared;
 static class Lazy { static int value=Production.lazy(); }
 @BeforeAll static void setup() { prepared=Production.prepared(); }
 @AfterAll static void cleanup() { Production.cleanup(); }
 @Test void alpha() { Assertions.assertEquals(6,initialized+prepared+Lazy.value); Production.alpha(); }
 @Test void beta() { Assertions.assertEquals(6,initialized+prepared+Lazy.value); }
 @org.junit.jupiter.params.ParameterizedTest @org.junit.jupiter.params.provider.ValueSource(ints={1})
 void overloaded(int n) { throw new AssertionError("audited overload must not execute"); }
 @org.junit.jupiter.params.ParameterizedTest @org.junit.jupiter.params.provider.ValueSource(strings={"x"})
 void overloaded(String n) { throw new AssertionError("audited overload must not execute"); }
}''')
    production=tmp_path/'work/checkouts/0/src/main/java/fixture/Production.java';production.parent.mkdir(parents=True)
    production.write_text('''package fixture;
public class Production {
 public static int initialized() { return 1; }
 public static int prepared() { return 2; }
 public static int lazy() { return 3; }
 public static void cleanup() { System.nanoTime(); }
 public static void alpha() { System.nanoTime(); }
}''')
    classes=tmp_path/'work/checkouts/0/target/classes';classes.mkdir(parents=True)
    (tmp_path/'work/runtime').mkdir(parents=True);(tmp_path/'work/classpath.txt').write_text('')
    write_json(tmp_path/'results/anchor.json',dict(sha='anchor'))
    write_json(tmp_path/'results/builds/0.json',dict(sha='anchor',build='ok',classpath='work/classpath.txt'))
    adapter=JavaMavenAdapter(tmp_path,config);members=[];expected={}
    with adapter.runner.session('candidate-setup-fixture'):
        assert adapter.runner.execute(['javac','-d',str(classes),str(production)],tmp_path,tmp_path/'production.log',60)['exit_code']==0
        for name in ('alpha','beta'):
            t=frozen_method(dict({'class':'fixture.Shared'},method=name,kind='plain'))
            t.update(source_root='dev/src-test-t/java',resource_root='dev/src-test-t',file_hash=hashlib.sha256(source.read_bytes()).hexdigest())
            members.append(t)
            result=adapter.method_coverage(t)
            assert result['methods'][0]['state']=='pass'
            rows=read_csv(tmp_path/f'results/dev-method-coverage-t/fixture.Shared#{name}-lines.csv')
            expected[name]={(r['file'],int(r['line'])) for r in rows if r['covered']=='yes'}
    baseline=[dict(t,stable='yes') for t in members]
    baseline += [dict({'class':'fixture.Shared'},method='overloaded',kind='parameterized',stable='no') for _ in range(2)]
    adapter.setup_runner=lambda:None  # Fixture JUnit/JaCoCo libraries are already provisioned.
    measured=candidate_coverage(tmp_path,config,adapter,baseline)
    assert len(measured['runs'])==1
    assert all(m['state']=='pass' for m in measured['methods'])
    assert {m['method']:{tuple(line) for line in m['lines']} for m in measured['methods']}==expected
    assert expected['alpha']-expected['beta']=={('src/main/java/fixture/Production.java',7)}
