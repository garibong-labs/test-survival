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
