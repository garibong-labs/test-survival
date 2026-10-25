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
def test_runner_setup_uses_qualified_plugin_and_retains_attempt_logs(tmp_path, monkeypatch, classifier):
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
    calls = []
    def execute(command, cwd, log, timeout):
        calls.append((command, log))
        assert command[2] == 'org.apache.maven.plugins:maven-dependency-plugin:3.6.1:get'
        coordinate = 'org.example:sample:1.0'+(':jar:'+classifier if classifier else '')
        assert '-Dartifact='+coordinate in command
        assert timeout == 123
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('failed attempt' if len(calls) == 1 else 'successful attempt')
        if len(calls) > 1:
            source.parent.mkdir(parents=True)
            source.write_bytes(payload)
        return dict(command=command, exit_code=1 if len(calls) == 1 else 0, timeout=False,
                    seconds=1, cwd=str(cwd), log=str(log.relative_to(tmp_path)))
    monkeypatch.setattr(worker, 'execute', execute)
    with pytest.raises(RuntimeError, match='runner-dependency-sample-1.txt'):
        worker.setup()
    worker.setup()
    assert calls[0][1].read_text() == 'failed attempt'
    assert calls[1][1].read_text() == 'successful attempt'
    assert calls[0][1] != calls[1][1]
    evidence = json.loads((tmp_path/'results/runner-dependency-sample.json').read_text())
    assert [attempt['exit_code'] for attempt in evidence['attempts']] == [1, 0]
    assert all((tmp_path/attempt['log']).exists() for attempt in evidence['attempts'])
    assert (tmp_path/'work/libs'/filename).read_bytes() == payload
    source.unlink()
    worker.setup()
    assert len(calls) == 2  # Verified experiment jars also remain reusable.
    assert json.loads((tmp_path/'results/runner-setup.json').read_text())['status'] == 'completed'
