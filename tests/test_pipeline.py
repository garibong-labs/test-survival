from copy import deepcopy
import os

import pytest

from test_survival.config import STAGES, validate
from test_survival.io import read_json
from test_survival.pipeline import PipelineError, run, run_lock, status
from test_survival.cli import main


@pytest.fixture
def config():
    return validate({'name': 'test', 'project': {'repo': '/project'}, 'anchor': 'v1'})


def fake_stages(seen):
    def handler(stage):
        def invoke(exp, config):
            seen.append(stage)
            return f'{stage} evidence ready'
        return invoke
    return {s: handler(s) for s in STAGES}


def test_resume_and_changed_hash(tmp_path, config):
    seen = []
    handlers = fake_stages(seen)
    assert run(tmp_path, config, until='scope', handlers=handlers) == ['prepare', 'scope']
    assert run(tmp_path, config, handlers=handlers) == list(STAGES[2:])
    assert run(tmp_path, config, handlers=handlers) == []
    modified = deepcopy(config)
    modified['screen']['matching'] = 'subsample-to-dev'
    assert run(tmp_path, modified, handlers=handlers) == list(STAGES[3:])
    assert read_json(tmp_path / 'state/report.json')['status'] == 'completed'
    assert 'report     completed' in status(tmp_path, modified)


def test_stage_prerequisites_force_invalidates_later(tmp_path, config):
    handlers = fake_stages([])
    with pytest.raises(PipelineError, match='requires'):
        run(tmp_path, config, stage='screen', handlers=handlers)
    run(tmp_path, config, handlers=handlers)
    assert run(tmp_path, config, stage='scope', force=True, handlers=handlers) == ['scope']
    assert read_json(tmp_path / 'state/report.json')['status'] == 'pending'
    assert run(tmp_path, config, handlers=handlers) == list(STAGES[2:])


def test_failed_stage_resumes_and_preserves_evidence(tmp_path, config):
    seen = []
    handlers = fake_stages(seen)
    def fail(exp, config):
        (exp / 'evidence.txt').write_text('partial')
        raise RuntimeError('failure')
    handlers['scope'] = fail
    with pytest.raises(RuntimeError):
        run(tmp_path, config, handlers=handlers)
    assert read_json(tmp_path / 'state/scope.json')['status'] == 'failed'
    assert (tmp_path / 'evidence.txt').read_text() == 'partial'
    assert run(tmp_path, config, until='scope', handlers=fake_stages(seen)) == ['scope']


def test_live_lock_refuses_second_run(tmp_path, config):
    with run_lock(tmp_path):
        with pytest.raises(PipelineError, match='already alive'):
            run(tmp_path, config, handlers=fake_stages([]))
    run(tmp_path, config, until='prepare', handlers=fake_stages([]))
    assert read_json(tmp_path / 'state/run.json')['pid'] == os.getpid()


def test_init_and_status(tmp_path, capsys):
    assert main(['init', str(tmp_path)]) == 0
    original = (tmp_path / 'experiment.yaml').read_text()
    assert '#' in original
    assert main(['init', str(tmp_path)]) == 2
    assert (tmp_path / 'experiment.yaml').read_text() == original
    assert main(['status', str(tmp_path)]) == 0
    assert 'prepare    pending' in capsys.readouterr().out


def test_detached_child_records_its_completion(tmp_path, config):
    import subprocess
    import time
    import yaml
    from test_survival.config import stage_hash
    from test_survival.io import write_json
    from test_survival.pipeline import background
    (tmp_path/'experiment.yaml').write_text(yaml.safe_dump(config))
    for stage in STAGES:
        write_json(tmp_path/'state'/f'{stage}.json', dict(status='completed', config_hash=stage_hash(config, stage)))
    pid, log_path = background(tmp_path)
    deadline = time.monotonic()+10
    while time.monotonic()<deadline:
        record = read_json(tmp_path/'state/run.json')
        if record['status']!='running':
            break
        time.sleep(.02)
    assert record['pid']==pid
    assert record['status']=='completed'
    assert record['log']==str(log_path)
    assert 'report: already completed; skipped' in log_path.read_text()
    # Reap our child so this test does not leave a zombie in a long-lived runner.
    os.waitpid(pid, 0)


def test_failure_before_evidence_leaves_exception_file(tmp_path, config, capsys):
    def fail(exp, config):
        raise RuntimeError('missing anchor v404')
    with pytest.raises(RuntimeError, match='missing anchor v404'):
        run(tmp_path, config, until='prepare', handlers={'prepare': fail})
    assert (tmp_path/'state/prepare-error.txt').read_text() == 'RuntimeError: missing anchor v404\n'
    assert 'state/prepare-error.txt' in read_json(tmp_path/'state/prepare.json')['notes']
    output = capsys.readouterr().out
    assert 'state/prepare-error.txt' in output
    assert 'missing anchor v404' not in output


@pytest.mark.parametrize('detached', [False, True])
def test_stop_terminates_recorded_run_marks_only_running_stage_and_releases_lock(tmp_path, detached):
    import subprocess
    import sys
    from test_survival.io import write_json
    write_json(tmp_path/'state/prepare.json', {'status': 'completed'})
    script = '''
import os, sys, time
from pathlib import Path
from test_survival.io import now, write_json
from test_survival.pipeline import run_lock
exp = Path(sys.argv[1])
with run_lock(exp):
    write_json(exp/'state/run.json', dict(pid=os.getpid(), status='running', started=now()))
    write_json(exp/'state/scope.json', dict(status='running', started=now()))
    print('ready', flush=True)
    time.sleep(30)
'''
    child = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)], stdout=subprocess.PIPE,
                             text=True, start_new_session=detached)
    try:
        assert child.stdout.readline().strip() == 'ready'
        inode = (tmp_path/'state/run.lock').stat().st_ino
        assert main(['stop', str(tmp_path)]) == 0
        child.wait(timeout=5)
        assert child.returncode < 0
        for path in ('run', 'scope'):
            record = read_json(tmp_path/f'state/{path}.json')
            assert record['status'] == 'failed'
            assert record['notes'] == 'stopped by user'
            assert record['finished']
        assert read_json(tmp_path/'state/prepare.json')['status'] == 'completed'
        with run_lock(tmp_path):
            assert (tmp_path/'state/run.lock').stat().st_ino == inode
        assert main(['stop', str(tmp_path)]) == 2
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_stop_recovers_dead_run_and_rejects_own_pid(tmp_path):
    import subprocess
    import sys
    from test_survival.io import write_json
    from test_survival.pipeline import stop
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait()
    write_json(tmp_path/'state/run.json', {'status': 'running', 'pid': child.pid})
    write_json(tmp_path/'state/survive.json', {'status': 'running'})
    assert stop(tmp_path) == child.pid
    with run_lock(tmp_path):
        pass
    write_json(tmp_path/'state/run.json', {'status': 'running', 'pid': os.getpid()})
    with pytest.raises(PipelineError, match='invalid run PID'):
        stop(tmp_path)
