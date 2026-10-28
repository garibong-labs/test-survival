import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from test_survival import lifecycle, pipeline
from test_survival.config import validate
from test_survival.io import read_json, write_json
from test_survival.runner.docker import DockerRunner


def wait_for(predicate):
    deadline = time.monotonic()+5
    while not predicate():
        assert time.monotonic() < deadline, 'Child did not become ready'
        time.sleep(.01)


def run_record(exp, child, **extra):
    record = dict(pid=child.pid, identity=lifecycle.process_identity(child.pid),
                  run_id='test-run', status='running', started='start')
    record.update(extra)
    write_json(exp/'state/run.json', record)
    write_json(exp/'state/survive.json', {'status': 'running'})
    return record


@pytest.mark.parametrize('identity', ['previous-process', None])
def test_stop_never_signals_reused_or_unverified_pid(tmp_path, identity):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    try:
        run_record(tmp_path, child, identity=identity)
        if identity is None:
            with pytest.raises(pipeline.PipelineError, match='unverified PID'):
                pipeline.stop(tmp_path)
            assert not (tmp_path/'state/stopping.json').exists()
        else:
            pipeline.stop(tmp_path)
            assert read_json(tmp_path/'state/run.json')['status'] == 'failed'
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_signals_recheck_identity(monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle, 'process_identity', lambda pid: 'replacement')
    monkeypatch.setattr(lifecycle.os, 'kill', lambda *args: calls.append(args))
    assert not lifecycle.signal_owned({'pid': 12345, 'identity': 'original'}, 9)
    assert calls == []


def test_stale_recovery_does_not_interrupt_a_new_lock_owner(tmp_path):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        run_record(tmp_path, child, identity='previous-process')
        with pipeline.run_lock(tmp_path):
            with pytest.raises(pipeline.PipelineError, match='unverified process'):
                pipeline.stop(tmp_path)
        assert not (tmp_path/'state/stopping.json').exists()
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_kernel_identity_is_stable_across_stop_and_continue():
    import signal
    children = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']) for _ in range(2)]
    try:
        identities = [lifecycle.process_identity(child.pid) for child in children]
        # Linux start times have tick resolution and can coincide across PIDs.
        assert all(identities)
        assert len({(child.pid, identity) for child, identity in zip(children, identities)}) == 2
        child = children[0]
        os.kill(child.pid, signal.SIGSTOP)
        assert lifecycle.process_identity(child.pid) == identities[0]
        os.kill(child.pid, signal.SIGCONT)
        assert lifecycle.process_identity(child.pid) == identities[0]
    finally:
        for child in children:
            child.kill()
            child.wait()
            assert lifecycle.process_identity(child.pid) is None


def test_stop_terminates_detached_workers_and_descendants_before_resume(tmp_path):
    # Exercise DockerRunner.execute's real separate-session process launch.
    # Only the Docker command is replaced, so no real containers are touched.
    script = '''
import os, sys
from pathlib import Path
from test_survival.config import validate
from test_survival.io import now, write_json
from test_survival.lifecycle import process_identity
from test_survival.pipeline import run_lock
from test_survival.runner.docker import DockerRunner
exp = Path(sys.argv[1])
with run_lock(exp):
    write_json(exp/'state/run.json', dict(pid=os.getpid(), identity=process_identity(os.getpid()), run_id='writers', status='running', started=now()))
    write_json(exp/'state/survive.json', dict(status='running'))
    runner = DockerRunner(exp, validate(dict(name='test', anchor='t', project=dict(repo=str(exp)))))
    runner.command = lambda *args: [sys.executable, str(exp/'worker.py'), str(exp)]
    runner.execute([], exp, exp/'state/worker.log', timeout=30)
'''
    (tmp_path/'worker.py').write_text('''
import os, subprocess, sys, time
from pathlib import Path
exp = Path(sys.argv[1])
if len(sys.argv) == 2:
    subprocess.Popen([sys.executable, __file__, str(exp), 'grandchild'], start_new_session=True)
name = 'grandchild' if len(sys.argv) == 3 else 'worker'
(exp/(name+'.pid')).write_text(str(os.getpid()))
while True:
    with (exp/(name+'.writes')).open('a') as stream:
        stream.write('writing\\n')
    time.sleep(.02)
''')
    child = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)], start_new_session=True)
    workers = []
    try:
        wait_for(lambda: (tmp_path/'grandchild.writes').exists())
        workers = [int((tmp_path/(name+'.pid')).read_text()) for name in ('worker', 'grandchild')]
        assert all(os.getpgid(pid) == pid for pid in workers)
        pipeline.stop(tmp_path)
        child.wait(timeout=5)
        assert all(lifecycle.process_identity(pid) is None for pid in workers)
        with pipeline.run_lock(tmp_path):
            before = (tmp_path/'grandchild.writes').read_text()
            time.sleep(.06)
            assert (tmp_path/'grandchild.writes').read_text() == before
        assert read_json(tmp_path/'state/survive.json')['notes'] == 'stopped by user'
    finally:
        for pid in workers:
            if lifecycle.process_identity(pid):
                os.kill(pid, 9)
        if child.poll() is None:
            child.kill()
        child.wait()


def test_cleanup_failure_blocks_resume_and_stop_can_retry(tmp_path, monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    run_record(tmp_path, child)
    attempts = []
    def cleanup(exp, record):
        attempts.append(record['run_id'])
        assert child.poll() is not None
        with pytest.raises(pipeline.PipelineError):
            with pipeline.run_lock(exp):
                pytest.fail('Lock admitted a new run before container cleanup')
        if len(attempts) == 1:
            raise RuntimeError('Docker is unavailable')
    monkeypatch.setattr(pipeline, 'cleanup_containers', cleanup)
    try:
        with pytest.raises(pipeline.PipelineError, match='resume blocked'):
            pipeline.stop(tmp_path)
        assert (tmp_path/'state/stopping.json').exists()
        with pytest.raises(pipeline.PipelineError, match='cleanup is incomplete'):
            with pipeline.run_lock(tmp_path):
                pytest.fail('Incomplete stop admitted a new run')
        pipeline.stop(tmp_path)
        assert attempts == ['test-run', 'test-run']
        assert not (tmp_path/'state/stopping.json').exists()
        with pipeline.run_lock(tmp_path):
            assert read_json(tmp_path/'state/run.json')['status'] == 'failed'
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


@pytest.mark.parametrize('compose', [False, True])
def test_container_launches_record_ownership_before_execution(tmp_path, compose):
    write_json(tmp_path/'state/run.json', {'run_id': 'owner'})
    docker = {'compose_files': ['/compose.yml'], 'service': 'ktester'} if compose else {'image': 'test-image'}
    config = validate(dict(name='test', anchor='t', project={'repo': '/repo'}, runner={'docker': docker}))
    command = DockerRunner(tmp_path, config).command(['sleep', '1'], tmp_path)
    name = command[command.index('--name')+1]
    assert command[command.index('--label')+1] == 'test-survival.run=owner'
    assert read_json(tmp_path/f'state/containers/{name}.json') == dict(name=name, run_id='owner')


@pytest.mark.parametrize('remains', [False, True])
def test_container_cleanup_is_scoped_and_verifies_removal(tmp_path, monkeypatch, remains):
    write_json(tmp_path/'state/containers/one.json', {'run_id': 'owner', 'name': 'one'})
    calls = []
    running = ['owned-container']
    def docker(command, **kwargs):
        calls.append(command)
        if command[1] == 'ps':
            assert command[-1] == 'label=test-survival.run=owner'
            return subprocess.CompletedProcess(command, 0, '\n'.join(running), '')
        assert command == ['docker', 'rm', '--force', 'owned-container']
        if not remains:
            running.clear()
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(lifecycle.subprocess, 'run', docker)
    if remains:
        with pytest.raises(RuntimeError, match='remain after cleanup'):
            lifecycle.cleanup_containers(tmp_path, {'run_id': 'owner'})
    else:
        lifecycle.cleanup_containers(tmp_path, {'run_id': 'owner'})
    assert len(calls) == 3


def test_stop_recovers_recorded_orphan_worker(tmp_path):
    worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    exited = subprocess.Popen([sys.executable, '-c', 'pass'])
    exited.wait()
    try:
        record = run_record(tmp_path, exited)
        write_json(tmp_path/'state/workers/old.json', dict(pid=worker.pid,
                   identity='previous-worker-with-same-pid', run_id=record['run_id']))
        write_json(tmp_path/'state/workers/orphan.json', dict(pid=worker.pid,
                   identity=lifecycle.process_identity(worker.pid), run_id=record['run_id']))
        pipeline.stop(tmp_path)
        worker.wait(timeout=5)
        assert worker.returncode < 0
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait()


def test_shared_container_cleanup_verifies_exit_and_preserves_state_on_failure(tmp_path, monkeypatch):
    path = tmp_path/'state/container-survive.json'
    ident = 'a'*64
    write_json(path, dict(container=ident, stage='survive', status='running'))
    available = False
    running = True
    def docker(command, **kwargs):
        nonlocal running
        if not available:
            return subprocess.CompletedProcess(command, 1, '', 'daemon unavailable')
        if command[1] == 'ps':
            assert command[-1] == 'id='+ident
            return subprocess.CompletedProcess(command, 0, ident if running else '', '')
        assert command == ['docker', 'rm', '--force', ident]
        running = False
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(lifecycle.subprocess, 'run', docker)
    with pytest.raises(RuntimeError, match='daemon unavailable'):
        lifecycle.cleanup_containers(tmp_path, {})
    assert read_json(path)['status'] == 'running'
    available = True
    lifecycle.cleanup_containers(tmp_path, {})
    assert not running
    assert read_json(path)['status'] == 'stopped'
    assert read_json(path)['finished']
