from pathlib import Path
import hashlib
import json
import pytest
from test_survival.adapters.java_worker import parse_reports,method_state


def test_launcher_names_parameterized_and_error_precedence(tmp_path):
    (tmp_path/'TEST-junit.xml').write_text('''<testsuite><testcase classname="p.T" name="display name"><system-out>[test-template:works(java.lang.String)]</system-out></testcase><testcase classname="p.T" name="works(String)[2]"><failure type="org.opentest4j.AssertionFailedError" message="expected"/></testcase><testcase classname="p.T" name="skip()"><skipped/></testcase></testsuite>''')
    obs=parse_reports(tmp_path)
    assert method_state({'class':'p.T','method':'works'},obs,{'timeout':False})==('fail','org.opentest4j.AssertionFailedError: expected')
    assert method_state({'class':'p.T','method':'skip'},obs,{'timeout':False})[0]=='absent'
    assert method_state({'class':'p.T','method':'missing'},obs,{'timeout':False})[0]=='absent'
    assert method_state({'class':'p.T','method':'works'},obs,{'timeout':True})[0]=='timeout'


@pytest.mark.parametrize('classifier', ['', 'runtime'])
@pytest.mark.parametrize('corrupt_cache', [False, True])
def test_runner_setup_uses_qualified_plugin_and_retains_attempt_logs(tmp_path, monkeypatch, classifier, corrupt_cache):
    from test_survival.adapters import java_worker as worker
    helpers = tmp_path/'helpers'
    helpers.mkdir()
    filename = 'sample-1.0'+('-'+classifier if classifier else '')+'.jar'
    payload = b'test jar contents'
    jar = {'filename': filename, 'coordinate': 'org.example:sample:1.0', 'classifier': classifier,
           'sha256': hashlib.sha256(payload).hexdigest(), 'role': 'test'}
    (helpers/'classpath-artifacts.json').write_text(json.dumps([jar]))
    for name, value in {'EXP': tmp_path, 'RESULTS': tmp_path/'results', 'HELPERS': helpers,
                        'REPO': tmp_path, 'TIMEOUTS': {'build': 123}}.items():
        monkeypatch.setattr(worker, name, value, raising=False)
    repository = tmp_path/'m2/repository'
    monkeypatch.setattr(worker, 'Path', lambda value: repository if value == '/root/.m2/repository' else Path(value))
    source = repository/'org/example/sample/1.0'/filename
    if corrupt_cache:
        source.parent.mkdir(parents=True)
        source.write_bytes(b'corrupt cache')
    calls = []
    def execute(command, cwd, log, timeout):
        assert not source.exists()
        calls.append((command, log))
        assert command[2:4] == ['-U', 'org.apache.maven.plugins:maven-dependency-plugin:3.6.1:get']
        coordinate = 'org.example:sample:1.0'+(':jar:'+classifier if classifier else '')
        assert '-Dartifact='+coordinate in command
        assert timeout == 123
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('failed attempt' if len(calls) == 1 else 'successful attempt')
        if len(calls) > 1:
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(payload)
        return dict(command=command, exit_code=1 if len(calls) == 1 else 0, timeout=False,
                    seconds=1, cwd=str(cwd), log=str(log.relative_to(tmp_path)))
    monkeypatch.setattr(worker, 'execute', execute)
    key = 'org.example.'+Path(filename).stem
    with pytest.raises(RuntimeError, match=f'runner-dependency-{key}-1.txt'):
        worker.setup()
    worker.setup()
    assert calls[0][1].read_text() == 'failed attempt'
    assert calls[1][1].read_text() == 'successful attempt'
    assert calls[0][1] != calls[1][1]
    evidence = json.loads((tmp_path/f'results/runner-dependency-{key}.json').read_text())
    assert [attempt['exit_code'] for attempt in evidence['attempts']] == [1, 0]
    assert evidence['attempts'][0]['rejected_sha256'] == (hashlib.sha256(b'corrupt cache').hexdigest() if corrupt_cache else None)
    assert evidence['attempts'][1]['downloaded_sha256'] == jar['sha256']
    assert all((tmp_path/attempt['log']).exists() for attempt in evidence['attempts'])
    assert (tmp_path/'work/libs'/filename).read_bytes() == payload
    source.unlink()
    worker.setup()
    assert len(calls) == 2  # Verified experiment jars also remain reusable.
    assert json.loads((tmp_path/'results/runner-setup.json').read_text())['status'] == 'completed'


def test_setup_separates_artifact_versions_and_rejects_bad_downloads(tmp_path, monkeypatch):
    from test_survival.adapters import java_worker as worker
    helpers = tmp_path/'helpers'
    helpers.mkdir()
    records = [dict(filename=f'sample-{version}{suffix}.jar', coordinate=f'{group}:sample:{version}',
                    classifier=classifier, sha256=hashlib.sha256(b'valid').hexdigest(), role='test')
               for group, version, classifier, suffix in [('org.one', '1.0', '', ''),
                   ('org.one', '2.0', '', ''), ('org.one', '2.0', 'runtime', '-runtime')]]
    (helpers/'classpath-artifacts.json').write_text(json.dumps(records))
    for name, value in {'EXP': tmp_path, 'RESULTS': tmp_path/'results', 'HELPERS': helpers,
                        'REPO': tmp_path, 'TIMEOUTS': {'build': 123}}.items():
        monkeypatch.setattr(worker, name, value, raising=False)
    repository = tmp_path/'m2/repository'
    monkeypatch.setattr(worker, 'Path', lambda value: repository if value == '/root/.m2/repository' else Path(value))
    bad_download = True
    def execute(command, cwd, log, timeout):
        coordinate = next(arg.removeprefix('-Dartifact=') for arg in command if arg.startswith('-Dartifact='))
        parts = coordinate.split(':')
        group, artifact, version = parts[:3]
        classifier = parts[4] if len(parts) == 5 else ''
        filename = artifact+'-'+version+('-'+classifier if classifier else '')+'.jar'
        source = repository/group.replace('.', '/')/artifact/version/filename
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b'bad download' if bad_download else b'valid')
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(coordinate)
        return dict(command=command, exit_code=0, log=str(log.relative_to(tmp_path)))
    monkeypatch.setattr(worker, 'execute', execute)
    with pytest.raises(RuntimeError, match='Runner jar checksum mismatch'):
        worker.setup()
    assert not (tmp_path/'work/libs/sample-1.0.jar').exists()
    bad_download = False
    worker.setup()
    evidence = sorted((tmp_path/'results').glob('runner-dependency-*.json'))
    assert len(evidence) == 3
    for path in evidence:
        data = json.loads(path.read_text())
        assert all(attempt['coordinate'] == data['coordinate'] for attempt in data['attempts'])
        assert data['attempts'][-1]['downloaded_sha256'] == hashlib.sha256(b'valid').hexdigest()
    first = json.loads((tmp_path/'results/runner-dependency-org.one.sample-1.0.json').read_text())
    assert len(first['attempts']) == 2
    assert first['attempts'][1]['rejected_sha256'] == hashlib.sha256(b'bad download').hexdigest()
