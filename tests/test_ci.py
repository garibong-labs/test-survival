import hashlib
import importlib.util
import io
from pathlib import Path
import re

import pytest
import yaml

from test_junit4 import java_environment


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('java_fixture', ROOT/'ci/java_fixture.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


@pytest.mark.parametrize('existing', [None, b'corrupt', b'verified jar'])
def test_fixture_libraries_verify_downloads_and_existing_cache(tmp_path, monkeypatch, existing):
    payload = b'verified jar'
    record = dict(filename='sample-1.0-runtime.jar', coordinate='org.example:sample:1.0',
                  classifier='runtime', sha256=hashlib.sha256(payload).hexdigest())
    target = tmp_path/record['filename']
    if existing is not None:
        target.write_bytes(existing)
    calls = []
    def download(url, timeout):
        calls.append(url)
        assert timeout == 60
        return io.BytesIO(payload)
    monkeypatch.setattr(fixture, 'urlopen', download)
    fixture.prepare_libraries(tmp_path, [record])
    assert target.read_bytes() == payload
    assert calls == ([] if existing == payload else [
        'https://repo.maven.apache.org/maven2/org/example/sample/1.0/sample-1.0-runtime.jar'])
    assert not list(tmp_path.glob('*.download'))


def test_fixture_checksum_mismatch_does_not_publish(tmp_path, monkeypatch):
    record = dict(filename='sample.jar', coordinate='org.example:sample:1.0', sha256='0'*64)
    monkeypatch.setattr(fixture, 'urlopen', lambda *args, **kwargs: io.BytesIO(b'wrong jar'))
    with pytest.raises(ValueError, match='Checksum mismatch'):
        fixture.prepare_libraries(tmp_path, [record])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('problem', ['skipped', 'failure', 'error', 'missing', 'duplicate', None])
def test_integration_report_requires_both_successful_fixtures(tmp_path, problem):
    names = sorted(fixture.FIXTURES)
    if problem == 'missing':
        names.pop()
    elif problem == 'duplicate':
        names[1] = names[0]
    cases = ''.join(f'<testcase name="{name}">'+(f'<{problem}/>' if problem in ['skipped', 'failure', 'error'] else '')
                    +'</testcase>' for name in names)
    report = tmp_path/'report.xml'
    report.write_text('<testsuites><testsuite>'+cases+'</testsuite></testsuites>')
    if problem:
        with pytest.raises(ValueError):
            fixture.verify_report(report)
    else:
        fixture.verify_report(report)


def test_required_java_environment_fails_instead_of_skipping(monkeypatch):
    monkeypatch.delenv('SURVIVAL_JAVA_TEST_IMAGE', raising=False)
    monkeypatch.delenv('SURVIVAL_JAVA_TEST_LIBS', raising=False)
    monkeypatch.setenv('SURVIVAL_REQUIRE_JAVA_TESTS', '1')
    with pytest.raises(pytest.fail.Exception, match='SURVIVAL_JAVA_TEST_IMAGE'):
        java_environment.__wrapped__()


def test_ci_permissions_triggers_and_pins():
    workflows = {}
    for path in (ROOT/'.github/workflows').glob('*.yml'):
        data = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        workflows[path.name] = data
        assert data['permissions'] == {'contents': 'read'}
        assert set(data['on']) == {'pull_request', 'push', 'workflow_dispatch'}
        assert data['on']['push']['branches'] == ['main']
        assert data['concurrency']['cancel-in-progress'] == 'true'
        for job in data['jobs'].values():
            assert 0 < int(job['timeout-minutes']) <= 15
            for step in job['steps']:
                if 'uses' in step:
                    assert re.fullmatch(r'actions/[\w-]+@[0-9a-f]{40}', step['uses'])
                if step.get('uses', '').startswith('actions/checkout@'):
                    assert step['with']['persist-credentials'] == 'false'
    assert workflows['python-tests.yml']['jobs']['python']['strategy']['matrix']['python'] == ['3.12', '3.14']
    java = workflows['java-integration.yml']['jobs']['java']
    assert java['env']['SURVIVAL_REQUIRE_JAVA_TESTS'] == '1'
    assert java['steps'][-1]['run'] == 'python ci/java_fixture.py report java-results.xml'
    for line in (ROOT/'ci/java.Dockerfile').read_text().splitlines():
        if line.startswith('FROM '):
            assert re.search(r'@sha256:[0-9a-f]{64}(?: |$)', line)
