from pathlib import Path
import pytest
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
    config['runner']['docker']['mounts'] = {'/host/home': '/root'}
    command = DockerRunner(tmp_path, config).command(['true'], tmp_path)
    assert '/host/home:/root' in command and 'other-m2:/root/.m2' not in command
    config['runner']['docker']['mounts'] = {'/host/other': '/root/.m2-other'}
    assert 'other-m2:/root/.m2' in DockerRunner(tmp_path, config).command(['true'], tmp_path)


def test_compose_owns_its_maven_mounts(tmp_path):
    config = validate({'name': 'compose', 'anchor': 't', 'project': {'repo': '/repo'},
                       'runner': {'docker': {'compose_files': ['/compose.yml']}}})
    runner = DockerRunner(tmp_path, config)
    assert not any('/root/.m2' in arg for arg in runner.command(['true'], tmp_path))
    runner.container_id = 'existing-container'
    assert '--volume' not in runner.command(['true'], tmp_path)


def test_build_fingerprint_preserves_pre_volume_records(tmp_path, monkeypatch):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.config import range_fingerprint
    from test_survival.io import write_json
    config = validate({'name': 'legacy', 'anchor': 't', 'project': {'repo': '/project'},
                       'runner': {'docker': {'image': 'survival-java:25'}}})
    adapter = JavaMavenAdapter(tmp_path, config)
    # Computed with the configuration and hashing code from main before this PR.
    legacy = '0d028b6b9083f04842db49ee7c2dcf99690ec29e711039f8ee6a19198da8fb41'
    assert adapter.build_fingerprint('abc') == legacy
    assert range_fingerprint(config) == '55641757971af87d17727947a96349e0d7ba28591a21168f60300d769f6f0ca9'
    record = {'sha': 'abc', 'build': 'ok', 'input_sha256': legacy}
    write_json(tmp_path/'results/builds/0.json', record)
    (adapter.folder(0)/'target/classes').mkdir(parents=True)
    def unexpected(*args):
        pytest.fail('A valid legacy build must not be checked out or rebuilt')
    monkeypatch.setattr(adapter, 'checkout', unexpected)
    assert adapter.build(0, 'abc') == record
    config['runner']['docker']['image'] = 'survival-java:17'
    assert adapter.build_fingerprint('abc') != legacy


def test_record_fingerprints_preserve_home_spelling(tmp_path, monkeypatch):
    from pathlib import Path
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.config import range_fingerprint
    original = Path.expanduser
    raw = {'name': 'portable', 'anchor': 't', 'project': {'repo': '~/project'},
           'runner': {'docker': {'compose_files': ['~/compose.yml'], 'mounts': {'~/project': '/repo'}},
                      'reuse': {'checkouts': '~/checkouts', 'classpath_cache': '~/cache'}}}
    fingerprints = []
    for home in (tmp_path/'one', tmp_path/'two'):
        monkeypatch.setattr(Path, 'expanduser', lambda p: home/str(p)[2:] if str(p).startswith('~/') else original(p))
        config = validate(raw)
        adapter = JavaMavenAdapter(tmp_path, config)
        fingerprints.append((adapter.build_fingerprint('abc'), range_fingerprint(config)))
    assert fingerprints[0] == fingerprints[1]
    config['range']['limit'] = 12
    assert range_fingerprint(config) != fingerprints[-1][1]
