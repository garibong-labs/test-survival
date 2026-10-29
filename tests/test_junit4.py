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
            '[pathlib.Path(base+suffix).unlink() for suffix in (".json", ".xml")]; '
            'pathlib.Path("work/runtime/environment-attempts.txt").unlink()'],
            tmp_path,tmp_path/'fixture-cache-reset.log',30)
        assert reset['exit_code']==0
        cold=adapter.method_coverage(row)
        assert cold['whole_frozen_suite_compile']['cache']['status']=='miss'
        assert cold['methods']==covered['methods']
        assert read_csv(coverage_base.with_name(coverage_base.name+'-lines.csv'))==warm_lines
    assert (tmp_path/'results/dev-method-coverage-t/fixture.EnvironmentProbe#transientBind.xml').exists()
    lines=read_csv(tmp_path/'results/dev-method-coverage-t/fixture.EnvironmentProbe#transientBind-lines.csv')
    assert any(r['file']=='src/main/java/fixture/Production.java' and r['covered']=='yes' for r in lines)


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
 @Test void works() { org.junit.jupiter.api.Assertions.assertTrue(Production.METHOD() > 0); }
}'''.replace('METHOD',method))
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
 call(['javac','-cp','/out/production:'+cp,'-d',str(classes),source])
 selector=Path('/out')/(version+'.selectors');selector.write_text('fixture.VintageTest\n' if version=='vintage' else 'fixture.VersionTest\tworks\n')
 runtime=':'.join([str(classes),'/out/helper','/out/production',helpercp])
 manifest.append('\t'.join(['/out/'+version+'/dumps','/out/production',str(selector),runtime]))
Path('/out/batches.tsv').write_text('\n'.join(manifest)+'\n')
call(['java','-javaagent:'+agent+'=output=none,dumponexit=false','-cp','/out/helper:'+helpercp,'PerTestCoverage','/out/batches.tsv'])
# Exercise the real Python worker orchestration with the same compiled dependencies.
sys.path.insert(0,'/survival-package')
from adapters import java_worker as worker, policy_coverage
worker.EXP=Path('/out');worker.SHARED=worker.EXP;worker.HELPERS=Path('/helpers')
worker.JVM={'jacoco':'512m','javac':'256m'};worker.TIMEOUTS={'compile':60,'test_class':60}
Path('/out/work').mkdir();Path('/out/work/libs').symlink_to('/libs')
worker.classpath=lambda index:(Path('/out'),'/out/production:'+cp)
worker.jacoco_version=lambda classes:'0.8.14'
Path('/out/target').mkdir();Path('/out/target/classes').symlink_to('/out/production')
methods=[dict(id=version,file_hash=version,file=version+'/VersionTest.java',source_root=version,
              resource_root='resources',**{'class':'fixture.VersionTest'},method='works') for version in ['one','two']]
policy_coverage.run(worker,dict(index=0,sha='fixture',methods=methods,work='worker',output='worker-result.json'))
result=json.loads(Path('/out/worker-result.json').read_text())
assert len(result['methods'])==2 and all(t['state']=='pass' for t in result['methods']),result
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
