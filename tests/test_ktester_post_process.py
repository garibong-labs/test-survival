import os
from pathlib import Path
import subprocess

import pytest

from test_survival.adapters import ktester_classpath as cp
from test_survival.io import read_json


@pytest.mark.java_integration
def test_offline_author_round(tmp_path):
    author, libs = os.getenv('KTESTER_FIXTURE_TOOL'), os.getenv('KTESTER_FIXTURE_LIBS')
    if not author or not libs:
        pytest.skip('Set KTESTER_FIXTURE_TOOL and KTESTER_FIXTURE_LIBS to local author and runner libraries')
    repo = Path(__file__).resolve().parents[1]
    subprocess.run([
        'docker', 'run', '--rm', '--pull=never', '--network=none',
        '-v', f'{repo}/src:/src:ro', '-v', f'{repo}/tests/fixtures/ktester:/scripts:ro',
        '-v', f'{author}:/author:ro', '-v', f'{tmp_path}:/fixture', '-v', f'{libs}:/fixture/work/libs:ro',
        '-e', 'PYTHONPATH=/src', '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'FIXTURE_KEY=offline-stub-only',
        '--entrypoint', 'python3', os.getenv('KTESTER_FIXTURE_IMAGE', 'ktester-replication:latest'),
        '/scripts/author_round.py'], check=True, timeout=180)
    archive = tmp_path/'generated/k/round1'
    post = read_json(archive/'run.json')['post_process']
    assert (post['status'], post['visited'], post['total'], post['skipped']) == ('completed', 4, 4, 1)
    assert 'MultiIfc is already defined' in post['error']
    rows = read_json(tmp_path/'population/dataset_info.json')['sample']['focal-methods']
    for row in rows[1:]:
        assert 'return 1;' in row['focal-method']
        repair = archive/'sample/fix'/row['id']/'repair_prompt_0.md'
        assert 'Corresponding execution feedback' in repair.read_text()
        assert row['method-name'] in (archive/'sample/context+prompts'/row['id']/'gencode_prompt.md').read_text()
    assert (archive/'generation.log').read_text().count('1 tests successful') == 3
    truncated = archive.parent/'round2'
    run = read_json(truncated/'run.json')
    assert run['status'] == 'failed'
    assert run['post_process'] == read_json(truncated/'funnel.json')['post_process']
    assert (run['post_process']['visited'], run['post_process']['total']) == (1, 4)


def test_project_versions_precede_runner_fallbacks(tmp_path):
    project = tmp_path/'sample'
    libs = project/'libs'
    libs.mkdir(parents=True)
    runner = tmp_path/'runner'
    runner.mkdir()
    manifest = read_json(Path(cp.__file__).with_name('java')/'classpath-artifacts.json')
    for artifact in manifest:
        (runner/artifact['filename']).touch()
    for name in ('junit-jupiter-api-5.11.4', 'junit-platform-commons-1.11.4', 'mockito-core-4.0',
                 'byte-buddy-agent-1.17.7'):
        (libs/(name+'.jar')).touch()
    supplemental = project/'target/ktester-test-libs'
    supplemental.mkdir(parents=True)
    (supplemental/'junit-platform-console-standalone-1.9.3.jar').touch()
    def resolve(version, destination):
        assert version == '1.11.4'
        destination.touch()
    record = cp.configure(project, runner, supplemental, resolve)
    assert set(record['added']) == {cp.CONSOLE+'-1.11.4.jar', 'mockito-junit-jupiter-5.24.0.jar',
                                    'byte-buddy-1.17.7.jar', 'objenesis-3.3.jar'}
    assert {p.name for p in supplemental.glob('*.jar')} == set(record['added'])
    entries = (project/'dependencies.txt').read_text().split(os.pathsep)
    assert entries[0] == 'target/classes'
    assert entries[1:5] == ['libs/'+p.name for p in sorted(libs.iterdir())]
    assert all((project/p).is_file() for p in entries[1:])
    (libs/'junit-platform-engine-1.9.3.jar').touch()
    with pytest.raises(ValueError, match='Inconsistent project JUnit'):
        cp.configure(project, runner, supplemental, resolve)
