from fnmatch import fnmatchcase
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

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


@pytest.mark.parametrize('problem', ['skipped', 'failure', 'error', 'missing', 'duplicate', 'extra', 'unexpected', None])
def test_integration_report_requires_all_successful_fixtures(tmp_path, problem):
    names = sorted(fixture.FIXTURES)
    if problem == 'missing':
        names.pop()
    elif problem == 'duplicate':
        names[1] = names[0]
    elif problem == 'extra':
        names.append('test_unexpected_fixture')
    elif problem == 'unexpected':
        names[0] = 'test_unexpected_fixture'
    cases = ''.join(f'<testcase name="{name}">'+(f'<{problem}/>' if problem in ['skipped', 'failure', 'error'] else '')
                    +'</testcase>' for name in names)
    report = tmp_path/'report.xml'
    report.write_text('<testsuites><testsuite>'+cases+'</testsuite></testsuites>')
    if problem:
        with pytest.raises(ValueError):
            fixture.verify_report(report)
    else:
        fixture.verify_report(report)


def test_report_guard_matches_workflow_fixture_collection():
    workflow = yaml.load((ROOT/'.github/workflows/java-integration.yml').read_text(), Loader=yaml.BaseLoader)
    command = next(step['run'] for step in workflow['jobs']['java']['steps']
                   if step.get('name') == 'Run real Java fixtures')
    args = shlex.split(command)
    assert args[:3] == ['python', '-m', 'pytest']
    assert args.count('--junitxml=java-results.xml') == 1
    # Use the workflow's actual paths/marker selection and parametrized node IDs.
    # Collection does not set up fixtures or start Docker/Java. Do not write a report.
    args.remove('--junitxml=java-results.xml')
    collected = subprocess.run([sys.executable, *args[1:], '--collect-only'], cwd=ROOT,
                               env={**os.environ, 'PYTEST_ADDOPTS': ''},
                               capture_output=True, text=True, timeout=30)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    names = [line.rsplit('::', 1)[-1] for line in collected.stdout.splitlines()
             if line.startswith('tests/') and '::' in line]
    assert len(names) == len(set(names)), 'Fixture report names must be unique'
    assert set(names) == fixture.FIXTURES, (
        f'Workflow fixtures missing from report guard: {sorted(set(names) - fixture.FIXTURES)}; '
        f'Report guard fixtures absent from workflow: {sorted(fixture.FIXTURES - set(names))}'
    )


def test_required_java_environment_fails_instead_of_skipping(monkeypatch):
    monkeypatch.delenv('SURVIVAL_JAVA_TEST_IMAGE', raising=False)
    monkeypatch.delenv('SURVIVAL_JAVA_TEST_LIBS', raising=False)
    monkeypatch.setenv('SURVIVAL_REQUIRE_JAVA_TESTS', '1')
    with pytest.raises(pytest.fail.Exception, match='SURVIVAL_JAVA_TEST_IMAGE'):
        java_environment.__wrapped__()


@pytest.mark.parametrize('event', ['pull_request', 'push'])
@pytest.mark.parametrize('changed_file, selected', [
    ('.github/workflows/python-tests.yml', True),
    ('.github/workflows/future-check.yaml', True),
    ('docs/result-formats.md', False),
])
def test_java_ci_selects_workflow_only_changes(event, changed_file, selected):
    workflow = yaml.load((ROOT/'.github/workflows/java-integration.yml').read_text(), Loader=yaml.BaseLoader)
    # These positive literal/prefix globs use the same matching subset as Actions.
    paths = workflow['on'][event]['paths']
    assert all(not pattern.startswith('!') for pattern in paths)
    assert any(fnmatchcase(changed_file, pattern) for pattern in paths) == selected


def test_ci_permissions_triggers_and_pins():
    workflows = {}
    for path in (ROOT/'.github/workflows').glob('*.yml'):
        data = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        workflows[path.name] = data
        if path.name != 'eli-review-request.yml':
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
    fixtures = next(step['run'] for step in java['steps'] if step.get('name') == 'Run real Java fixtures')
    assert 'tests/test_compiler_session.py' in fixtures and 'tests/test_junit4.py' in fixtures
    assert java['steps'][-1]['run'] == 'python ci/java_fixture.py report java-results.xml'
    for line in (ROOT/'ci/java.Dockerfile').read_text().splitlines():
        if line.startswith('FROM '):
            assert re.search(r'@sha256:[0-9a-f]{64}(?: |$)', line)


def test_eli_review_notification_workflow_security_contract():
    workflow = yaml.load((ROOT/'.github/workflows/eli-review-request.yml').read_text(),
                         Loader=yaml.BaseLoader)
    # Exact keys also prevent extra triggers, permission overrides, cancellation,
    # environment injection, or additional jobs/steps outside this contract.
    assert set(workflow) == {'name', 'on', 'permissions', 'jobs'}
    assert workflow['name'] == 'Eli review request notification'
    assert workflow['permissions'] == {'contents': 'read', 'pull-requests': 'read'}
    assert workflow['on'] == {
        'pull_request_target': {'branches': ['main'], 'types': ['review_requested']},
        'workflow_dispatch': '',
    }
    assert set(workflow['jobs']) == {'notify'}
    notify = workflow['jobs']['notify']
    assert set(notify) == {'if', 'runs-on', 'timeout-minutes', 'steps'}
    # Compare the whole expression, preserving AND/OR grouping and branch gates.
    expected_gate = """
        github.repository == 'garibong-labs/test-survival' &&
        github.actor == 'Hamjoon' && github.run_attempt == 1 &&
        ((github.event_name == 'pull_request_target' &&
          github.event.action == 'review_requested' &&
          github.event.pull_request.base.ref == 'main' &&
          github.event.requested_reviewer.login == 'agent-eli' &&
          github.event.pull_request.state == 'open' &&
          github.event.pull_request.draft == false) ||
         (github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'))
    """
    assert ' '.join(notify['if'].split()) == ' '.join(expected_gate.split())
    assert notify['runs-on'] == 'ubuntu-latest'
    assert notify['timeout-minutes'] == '2'
    assert notify['steps'] == [
        {
            'name': 'Read isolated helper from trusted base commit',
            'uses': 'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
            'with': {
                'ref': '${{ github.sha }}',
                'persist-credentials': 'false',
                'sparse-checkout-cone-mode': 'false',
                'sparse-checkout': '/.github/scripts/review_request_discord.py',
            },
        },
        {
            'name': 'Verify live request and notify Discord',
            'run': 'python3 -I .github/scripts/review_request_discord.py',
            'env': {
                'GH_TOKEN': '${{ github.token }}',
                'ELI_REVIEW_DISCORD_WEBHOOK_URL': '${{ secrets.ELI_REVIEW_DISCORD_WEBHOOK_URL }}',
            },
        },
    ]
