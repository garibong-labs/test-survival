from pathlib import Path
from test_survival.config import validate
from test_survival.runner.docker import DockerRunner
from test_survival.runner.cache import ClasspathCache


def test_longest_mount_and_argv_are_preserved(tmp_path):
    c=validate({'name':'x','anchor':'v1','project':{'repo':'/host/project'},'runner':{'docker':{'image':'java-test','mounts':{'/host':'/root','/host/project':'/repo'}}}})
    r=DockerRunner(tmp_path,c)
    assert r.path('/host/project/src/A.java')=='/repo/src/A.java'
    command=r.command(['javac','-cp','/host/project/classes:/host/lib.jar','/host/project/a b.java'],'/host/project')
    assert command[-4:]==['javac','-cp','/repo/classes:/root/lib.jar','/repo/a b.java']


def test_classpath_resolution_once_per_pom(tmp_path):
    c=validate({'name':'x','anchor':'v1','project':{'repo':'/host/project'}})
    calls=[]
    class Runner:
        def execute(self, cmd, cwd, log, timeout):
            calls.append(cmd)
            path=Path(next(a.split('=',1)[1] for a in cmd if a.startswith('-Dmdep.outputFile=')))
            path.write_text('/m2/test.jar')
            return {'exit_code':0}
    folder=tmp_path/'project';folder.mkdir();(folder/'pom.xml').write_text('<project/>')
    cache=ClasspathCache(tmp_path,c,Runner())
    first=cache.resolve(folder,0)
    second=cache.resolve(folder,1)
    assert first[:2]==second[:2]
    assert len(calls)==1
    (folder/'pom.xml').write_text('<project><version>2</version></project>')
    assert cache.resolve(folder,2)[0]!=first[0]
    assert len(calls)==2


def test_image_mode_reuses_named_maven_volume_and_honors_explicit_mount(tmp_path):
    config = validate({'name': 'm2', 'anchor': 't', 'project': {'repo': '/repo'},
                       'runner': {'docker': {'image': 'survival-java:25'}}})
    command = DockerRunner(tmp_path, config).command(['mvn', '--version'], tmp_path)
    assert command[command.index('survival-m2:/root/.m2')-1] == '--volume'
    config['runner']['docker']['m2_volume'] = 'other-m2'
    assert 'other-m2:/root/.m2' in DockerRunner(tmp_path, config).command(['true'], tmp_path)
    config['runner']['docker']['mounts']['/host/m2'] = '/root/.m2'
    command = DockerRunner(tmp_path, config).command(['true'], tmp_path)
    assert '/host/m2:/root/.m2' in command and 'other-m2:/root/.m2' not in command


def test_compose_owns_its_maven_mounts(tmp_path):
    config = validate({'name': 'compose', 'anchor': 't', 'project': {'repo': '/repo'},
                       'runner': {'docker': {'compose_files': ['/compose.yml']}}})
    runner = DockerRunner(tmp_path, config)
    assert not any('/root/.m2' in arg for arg in runner.command(['true'], tmp_path))
    runner.container_id = 'existing-container'
    assert '--volume' not in runner.command(['true'], tmp_path)
