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
        # Force a fresh method result and compilation within this temporary fixture only.
        shutil.rmtree(tmp_path/'work/compile-cache')
        coverage_base.with_name(coverage_base.name+'.json').unlink()
        coverage_base.with_name(coverage_base.name+'.xml').unlink()
        (tmp_path/'work/runtime/environment-attempts.txt').unlink()
        cold=adapter.method_coverage(row)
        assert cold['whole_frozen_suite_compile']['cache']['status']=='miss'
        assert cold['methods']==covered['methods']
        assert read_csv(coverage_base.with_name(coverage_base.name+'-lines.csv'))==warm_lines
    assert (tmp_path/'results/dev-method-coverage-t/fixture.EnvironmentProbe#transientBind.xml').exists()
    lines=read_csv(tmp_path/'results/dev-method-coverage-t/fixture.EnvironmentProbe#transientBind-lines.csv')
    assert any(r['file']=='src/main/java/fixture/Production.java' and r['covered']=='yes' for r in lines)
