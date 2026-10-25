from pathlib import Path
import hashlib
import json
import pytest
from test_survival.adapters.java_worker import parse_reports,method_state


@pytest.fixture
def runner_jars_only(tmp_path, monkeypatch):
    from test_survival.adapters import maven_dependencies
    (tmp_path/'pom.xml').write_text('<project/>')
    monkeypatch.setattr(maven_dependencies, 'resolve', lambda *args, **kwargs: [])


def test_launcher_names_parameterized_and_error_precedence(tmp_path):
    (tmp_path/'TEST-junit.xml').write_text('''<testsuite><testcase classname="p.T" name="display name"><system-out>[test-template:works(java.lang.String)]</system-out></testcase><testcase classname="p.T" name="works(String)[2]"><failure type="org.opentest4j.AssertionFailedError" message="expected"/></testcase><testcase classname="p.T" name="skip()"><skipped/></testcase></testsuite>''')
    obs=parse_reports(tmp_path)
    assert method_state({'class':'p.T','method':'works'},obs,{'timeout':False})==('fail','org.opentest4j.AssertionFailedError: expected')
    assert method_state({'class':'p.T','method':'skip'},obs,{'timeout':False})[0]=='absent'
    assert method_state({'class':'p.T','method':'missing'},obs,{'timeout':False})[0]=='absent'
    assert method_state({'class':'p.T','method':'works'},obs,{'timeout':True})[0]=='timeout'


@pytest.mark.parametrize('classifier', ['', 'runtime'])
@pytest.mark.parametrize('corrupt_cache', [False, True])
def test_runner_setup_uses_qualified_plugin_and_retains_attempt_logs(tmp_path, monkeypatch, classifier, corrupt_cache, runner_jars_only):
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


def test_setup_separates_artifact_versions_and_rejects_bad_downloads(tmp_path, monkeypatch, runner_jars_only):
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


@pytest.mark.parametrize('diagnostic,failures,succeeds', [
    ('HTTP 429 Too Many Requests', 1, True),
    ('Could not transfer artifact', 3, True),
    ('HTTP 429 Too Many Requests', 4, False),
    ('Could not transfer artifact', 4, False),
    ('No plugin found for prefix', 1, False),
    ('Could not find artifact org.foo:bar:jar:1.4.29', 1, False),
    ('HTTP 429 mentioned in a successful command', 0, True),
])
def test_setup_retries_transfers_and_resolves_all_jars_sequentially(tmp_path, monkeypatch, diagnostic, failures, succeeds, runner_jars_only):
    from test_survival.adapters import java_worker as worker
    records = json.loads((Path(worker.__file__).parent/'java/classpath-artifacts.json').read_text())
    assert records
    payload = b'verified fixture jar'
    for record in records:
        record['sha256'] = hashlib.sha256(payload).hexdigest()
    helpers = tmp_path/'helpers'
    helpers.mkdir()
    (helpers/'classpath-artifacts.json').write_text(json.dumps(records))
    for name, value in {'EXP': tmp_path, 'RESULTS': tmp_path/'results', 'HELPERS': helpers,
                        'REPO': tmp_path, 'TIMEOUTS': {'build': 123}}.items():
        monkeypatch.setattr(worker, name, value, raising=False)
    repository = tmp_path/'m2/repository'
    monkeypatch.setattr(worker, 'Path', lambda value: repository if value == '/root/.m2/repository' else Path(value))
    coordinates = [r['coordinate']+(':jar:'+r['classifier'] if r.get('classifier') else '') for r in records]
    calls, sleeps = [], []
    def execute(command, cwd, log, timeout):
        coordinate = next(arg.removeprefix('-Dartifact=') for arg in command if arg.startswith('-Dartifact='))
        index = coordinates.index(coordinate)
        # No later jar may start until every previous jar has been verified and copied.
        assert all((tmp_path/'work/libs'/r['filename']).read_bytes() == payload for r in records[:index])
        calls.append(coordinate)
        failed = index == 0 and calls.count(coordinate) <= failures
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(diagnostic if failed or not failures else 'resolved')
        if not failed:
            group, artifact, version = records[index]['coordinate'].split(':')
            source = repository/group.replace('.', '/')/artifact/version/records[index]['filename']
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(payload)
        return dict(exit_code=int(failed), log=str(log.relative_to(tmp_path)))
    def sleep(seconds):
        sleeps.append(seconds)
        evidence = next((tmp_path/'results').glob('runner-dependency-*.json'))
        assert len(json.loads(evidence.read_text())['attempts']) == len(calls)
    monkeypatch.setattr(worker, 'execute', execute)
    if succeeds:
        worker.setup(wait=sleep)
        assert calls == [coordinates[0]]*failures + coordinates
        assert json.loads((tmp_path/'results/runner-setup.json').read_text())['status'] == 'completed'
    else:
        with pytest.raises(RuntimeError, match='Cannot resolve runner jar'):
            worker.setup(wait=sleep)
        assert calls == [coordinates[0]]*(4 if failures == 4 else 1)
        assert not (tmp_path/'results/runner-setup.json').exists()
    assert sleeps == [60]*(min(failures, 3) if succeeds or failures == 4 else 0)
    attempts = [attempt for path in (tmp_path/'results').glob('runner-dependency-*.json')
                for attempt in json.loads(path.read_text())['attempts']]
    assert len(attempts) == len(calls)
    assert len({attempt['log'] for attempt in attempts}) == len(calls)
    assert all((tmp_path/attempt['log']).exists() for attempt in attempts)


def test_retry_records_only_the_cache_rejected_by_that_attempt(tmp_path, monkeypatch, runner_jars_only):
    from test_survival.adapters import java_worker as worker
    payload = b'valid jar'
    jar = dict(filename='sample-1.0.jar', coordinate='org.example:sample:1.0',
               sha256=hashlib.sha256(payload).hexdigest(), role='test')
    helpers = tmp_path/'helpers'
    helpers.mkdir()
    (helpers/'classpath-artifacts.json').write_text(json.dumps([jar]))
    for name, value in {'EXP': tmp_path, 'RESULTS': tmp_path/'results', 'HELPERS': helpers,
                        'REPO': tmp_path, 'TIMEOUTS': {'build': 123}}.items():
        monkeypatch.setattr(worker, name, value, raising=False)
    repository = tmp_path/'m2/repository'
    monkeypatch.setattr(worker, 'Path', lambda value: repository if value == '/root/.m2/repository' else Path(value))
    source = repository/'org/example/sample/1.0/sample-1.0.jar'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'initial corrupt cache')
    calls, sleeps = [], []
    def execute(command, cwd, log, timeout):
        assert not source.exists()
        calls.append(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('Could not transfer' if len(calls) < 3 else 'success')
        if len(calls) == 1:
            source.write_bytes(b'partial download')
        elif len(calls) == 3:
            source.write_bytes(payload)
        return dict(exit_code=int(len(calls) < 3), log=str(log.relative_to(tmp_path)))
    monkeypatch.setattr(worker, 'execute', execute)
    worker.setup(wait=sleeps.append)
    attempts = json.loads((tmp_path/'results/runner-dependency-org.example.sample-1.0.json').read_text())['attempts']
    assert [a['rejected_sha256'] for a in attempts] == [
        hashlib.sha256(b'initial corrupt cache').hexdigest(),
        hashlib.sha256(b'partial download').hexdigest(), None]
    assert sleeps == [60, 60]
    assert (tmp_path/'work/libs/sample-1.0.jar').read_bytes() == payload


def test_setup_removes_jars_a_previous_manifest_installed_and_keeps_foreign_files(tmp_path, monkeypatch, runner_jars_only):
    from test_survival.adapters import java_worker as worker
    payload = b'current jar'
    current = dict(filename='sample-2.0.jar', coordinate='org.example:sample:2.0', sha256=hashlib.sha256(payload).hexdigest(), role='test')
    superseded = dict(filename='sample-1.0.jar', coordinate='org.example:sample:1.0', sha256=hashlib.sha256(b'old').hexdigest(), role='test')
    helpers = tmp_path/'helpers'
    helpers.mkdir()
    (helpers/'classpath-artifacts.json').write_text(json.dumps([current]))
    for name, value in {'EXP': tmp_path, 'RESULTS': tmp_path/'results', 'HELPERS': helpers,
                        'REPO': tmp_path, 'TIMEOUTS': {'build': 123}}.items():
        monkeypatch.setattr(worker, name, value, raising=False)
    libs = tmp_path/'work/libs'
    libs.mkdir(parents=True)
    (libs/'sample-2.0.jar').write_bytes(payload)
    (libs/'sample-1.0.jar').write_bytes(b'old')
    (libs/'dropped-in-by-hand.jar').write_bytes(b'not ours')
    (tmp_path/'results').mkdir()
    (tmp_path/'results/runner-setup.json').write_text(json.dumps({'status': 'completed', 'jars': [superseded]}))
    monkeypatch.setattr(worker, 'execute', lambda *args: pytest.fail('no download needed'))
    worker.setup()
    assert not (libs/'sample-1.0.jar').exists()
    assert (libs/'sample-2.0.jar').read_bytes() == payload
    assert (libs/'dropped-in-by-hand.jar').exists()  # Only jars this tool installed are pruned.
    assert json.loads((tmp_path/'results/runner-setup.json').read_text())['jars'] == [current]


def test_runner_test_jars_orders_console_launcher_last_and_skips_coverage():
    from test_survival.adapters.java_worker import runner_test_jars, project_jars
    records = [dict(filename='junit-platform-console-standalone-1.9.3.jar', role='test'),
               dict(filename='mockito-core-5.24.0.jar', role='test'),
               dict(filename='org.jacoco.agent-0.8.14-runtime.jar', role='coverage'),
               dict(filename='junit-jupiter-api-5.10.2.jar', role='test')]
    assert runner_test_jars(records) == ['mockito-core-5.24.0.jar', 'junit-jupiter-api-5.10.2.jar', 'junit-platform-console-standalone-1.9.3.jar']
    assert project_jars('/m2/org/junit/jupiter/x.jar:/m2/org/slf4j/slf4j.jar::/m2/junit/junit/junit-4.13.2.jar:/m2/org/mockito/mockito-core-4.0.0.jar\n') == ['/m2/org/slf4j/slf4j.jar', '/m2/org/mockito/mockito-core-4.0.0.jar']
