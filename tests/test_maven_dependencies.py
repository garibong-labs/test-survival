import hashlib
from pathlib import Path

import pytest

from test_survival.adapters import java_worker as worker, maven_dependencies as dependencies
from test_survival.io import read_json, write_json


@pytest.mark.parametrize('cached', [False, True])
def test_resolve_recovers_volume_and_retries_429(tmp_path, cached):
    cache = tmp_path/'classpaths/pom.txt'
    jar = tmp_path/'m2/slf4j.jar'
    cache.parent.mkdir()
    if cached:
        cache.write_text('/missing/old-volume.jar')
    calls, sleeps = [], []
    def execute(command, cwd, log, timeout):
        calls.append(command)
        log.parent.mkdir(parents=True, exist_ok=True)
        goal = next(c for c in command if c.startswith('dependency:'))
        online_attempts = sum(goal in c and '-o' not in c for c in calls)
        fail = '-o' in command or online_attempts == 1
        log.write_text('HTTP 429 Too Many Requests' if fail else 'resolved')
        if not fail and goal == 'dependency:build-classpath':
            jar.parent.mkdir(exist_ok=True);jar.touch()
            Path(next(c.split('=', 1)[1] for c in command if c.startswith('-Dmdep.outputFile='))).write_text(str(jar))
        return {'exit_code': int(fail), 'log': str(log)}
    attempts = dependencies.resolve(tmp_path, cache, tmp_path/'logs', 60, execute, wait=sleeps.append)
    assert cache.read_text() == str(jar)
    assert len(attempts) == (3 if cached else 6)
    assert sleeps == [60]*(1 if cached else 2)
    assert dependencies.resolve(tmp_path, cache, tmp_path/'logs', 60, execute) == []
    assert all(Path(a['log']).exists() for a in attempts)


@pytest.mark.parametrize('failure', ['command', 'missing-jar'])
def test_failed_rebuild_never_publishes_classpath(tmp_path, failure):
    cache = tmp_path/'pom.txt';cache.write_text('/missing/original.jar')
    def execute(command, cwd, log, timeout):
        log.parent.mkdir(parents=True, exist_ok=True);log.write_text('invalid dependency')
        output = Path(next(c.split('=', 1)[1] for c in command if c.startswith('-Dmdep.outputFile=')))
        output.write_text('/missing/replacement.jar')
        return {'exit_code': int(failure == 'command')}
    with pytest.raises(RuntimeError):
        dependencies.resolve(tmp_path, cache, tmp_path/'logs', 60, execute)
    assert cache.read_text() == '/missing/original.jar'
    assert not list(tmp_path.glob('*.tmp'))


def test_setup_resolves_anchor_after_jars_and_upgrades_old_marker(tmp_path, monkeypatch):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.config import validate
    config = validate({'name': 'x', 'anchor': 't', 'project': {'repo': '/repo'}})
    adapter = JavaMavenAdapter(tmp_path, config)
    folder = adapter.folder(0);folder.mkdir(parents=True);(folder/'pom.xml').write_text('<project/>')
    helpers = tmp_path/'helpers';helpers.mkdir()
    payload = b'runner jar'
    records = [dict(filename='test.jar', sha256=hashlib.sha256(payload).hexdigest(), role='test')]
    write_json(helpers/'classpath-artifacts.json', records)
    adapter.helpers = helpers
    libs = tmp_path/'work/libs';libs.mkdir();(libs/'test.jar').write_bytes(payload)
    write_json(tmp_path/'results/runner-setup.json', {'status': 'completed', 'jars': records})
    for key, value in dict(EXP=tmp_path, RESULTS=tmp_path/'results', HELPERS=helpers, REPO=folder, TIMEOUTS={'build': 60}).items():
        monkeypatch.setattr(worker, key, value, raising=False)
    calls = []
    def execute(command, cwd, log, timeout):
        assert (libs/'test.jar').read_bytes() == payload
        assert cwd == folder
        calls.append(command)
        if 'dependency:build-classpath' in command:
            Path(next(c.split('=', 1)[1] for c in command if c.startswith('-Dmdep.outputFile='))).write_text('')
        return {'exit_code': 0}
    monkeypatch.setattr(worker, 'execute', execute)
    monkeypatch.setattr(adapter, 'worker', lambda job: worker.setup())
    adapter.setup_runner()
    assert calls[0] == ['mvn', '-q', '-o', 'dependency:go-offline']
    assert 'dependency:build-classpath' in calls[1]
    adapter.setup_runner()
    assert len(calls) == 2
    assert read_json(tmp_path/'results/runner-setup.json')['project_dependencies'] == hashlib.sha256(b'<project/>').hexdigest()


def test_worker_checks_all_cached_entries_before_filtering_junit(tmp_path, monkeypatch):
    folder = tmp_path/'work/checkouts/0';folder.mkdir(parents=True)
    cache = tmp_path/'work/classpaths/pom.txt';cache.parent.mkdir()
    cache.write_text('/missing/org/junit/junit.jar:/missing/slf4j.jar')
    write_json(tmp_path/'results/builds/0.json', {'classpath': 'work/classpaths/pom.txt'})
    helpers = tmp_path/'helpers';helpers.mkdir();write_json(helpers/'classpath-artifacts.json', [])
    for key, value in dict(EXP=tmp_path, RESULTS=tmp_path/'results', HELPERS=helpers, MODULE='', TIMEOUTS={'build': 60}).items():
        monkeypatch.setattr(worker, key, value, raising=False)
    jar = tmp_path/'slf4j.jar';jar.touch()
    def execute(command, cwd, log, timeout):
        assert 'dependency:build-classpath' in command
        Path(next(c.split('=', 1)[1] for c in command if c.startswith('-Dmdep.outputFile='))).write_text(str(jar))
        return {'exit_code': 0}
    monkeypatch.setattr(worker, 'execute', execute)
    assert worker.classpath(0) == (folder, str(folder/'target/classes')+':'+str(jar))
