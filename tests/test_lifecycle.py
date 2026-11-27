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
        clock = iter([0, 30])
        monkeypatch.setattr(lifecycle.time, 'monotonic', lambda: next(clock))
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


def test_nested_pool_owns_workers_and_containers_at_coordinator(tmp_path):
    root=tmp_path/'experiment';nested=root/'results/policy/pools/dev/1'
    with pipeline.run_lock(root), pipeline.run_session(root) as record:
        write_json(nested/'state/run-owner.json',dict(experiment=str(root),run_id=record['run_id']))
        options=lifecycle.container_options(nested)
        assert 'test-survival.run='+record['run_id'] in options
        assert len(list((root/'state/containers').glob('*.json')))==1
        assert not (nested/'state/containers').exists()
        worker=lifecycle.launch(nested,[sys.executable,'-c','pass'])
        worker.wait(timeout=5)
        saved=[read_json(p) for p in (root/'state/workers').glob('*.json')]
        assert len(saved)==1 and saved[0]['run_id']==record['run_id'] and saved[0]['pid']==worker.pid
        write_json(root/'state/stopping.json',{})
        with pytest.raises(RuntimeError,match='stop cleanup'):
            lifecycle.launch(nested,[sys.executable,'-c','pass'])
        with pytest.raises(RuntimeError,match='stop cleanup'):
            lifecycle.container_options(nested)
        (root/'state/stopping.json').unlink()
    with pytest.raises(RuntimeError,match='inactive run'):
        lifecycle.container_options(nested)


@pytest.mark.parametrize('message', ['removal of container one is already in progress', 'No such container: one'])
def test_cleanup_polls_until_auto_removed_container_disappears(tmp_path, monkeypatch, message):
    write_json(tmp_path/'state/containers/one.json', dict(run_id='owner'))
    listings = iter(['one', 'one', ''])
    def docker(command, **kwargs):
        if command[1] == 'ps':
            assert command[-1] == 'label=test-survival.run=owner'
            return subprocess.CompletedProcess(command, 0, next(listings), '')
        assert command == ['docker', 'rm', '--force', 'one']
        return subprocess.CompletedProcess(command, 1, '', 'Error response from daemon: '+message)
    monkeypatch.setattr(lifecycle.subprocess, 'run', docker)
    monkeypatch.setattr(lifecycle.time, 'sleep', lambda seconds: None)
    lifecycle.cleanup_containers(tmp_path, dict(run_id='owner'))
    assert list(listings) == []


def run_model_workers(exp, workspace, drain):
    from concurrent.futures import ThreadPoolExecutor
    from io import BytesIO
    from unittest.mock import patch
    from test_survival.model.openrouter import OpenRouter

    with pipeline.run_lock(exp), pipeline.run_session(exp) as run:
        if workspace != exp:
            write_json(workspace/'state/run-owner.json', dict(experiment=str(exp), run_id=run['run_id']))
        write_json(exp/'state/generate.json', dict(status='running'))
        os.environ['TEST_MODEL_KEY'] = 'fake-offline-test-key'
        client = OpenRouter(dict(name='test', env_key='TEST_MODEL_KEY'), exp=workspace)
        params = dict(temperature=0, max_tokens=10)
        def http(request, timeout):
            assert not (exp/'http-started').exists(), 'Late call reached HTTP'
            (exp/'http-started').touch()
            if drain:
                wait_for(lambda: (exp/'late-blocked').exists())
            else:
                time.sleep(60)
            result = BytesIO(b'{"choices":[{"message":{"content":"done"}}]}')
            result.status = 200
            return result
        def late():
            wait_for(lambda: (exp/'state/stopping.json').exists())
            with pytest.raises(RuntimeError, match='stopped by user'):
                client.complete('late', params, workspace/'late')
            (exp/'late-blocked').touch()
        with patch('urllib.request.urlopen', http), ThreadPoolExecutor(2) as pool:
            first = pool.submit(client.complete, 'first', params, workspace/'first')
            if drain:
                pool.submit(late).result()
            first.result()
        time.sleep(60)  # Other stage work must not delay stop.


@pytest.mark.parametrize(('options', 'policy_pool'), [([], False), ([], True), (['--wait', '0.1'], False),
                                                      (['--no-wait'], False)])
def test_cli_stops_threaded_native_calls_and_can_immediately_resume(tmp_path, options, policy_pool, monkeypatch, capsys):
    import multiprocessing
    from test_survival.cli import main
    workspace = tmp_path/'results/policy/pools/native/1' if policy_pool else tmp_path
    drain = not options
    child = multiprocessing.get_context('spawn').Process(target=run_model_workers, args=(tmp_path, workspace, drain))
    child.start()
    try:
        wait_for(lambda: (tmp_path/'http-started').exists())
        assert main(['stop', str(tmp_path), *options]) == 0
        child.join(5)
        assert child.exitcode is not None
        output = capsys.readouterr().out
        if drain:
            assert 'Waiting up to 300s for 1' in output
            assert '1 finished during stop, 0 still in flight' in output
            assert read_json(workspace/'first/run.json')['status'] == 'ok'
            assert (tmp_path/'late-blocked').is_file()
            assert not (workspace/'late').exists()
        else:
            assert '0 finished during stop, 1 still in flight' in output
            assert ('Waiting up to' in output) == ('--wait' in options)
            assert [p.name for p in (workspace/'first').iterdir()] == ['request.json']
        assert read_json(tmp_path/'state/generate.json')['notes'] == 'stopped by user'
        write_json(tmp_path/'experiment.yaml', dict(name='fixture', anchor='t', project=dict(repo=str(tmp_path))))
        monkeypatch.setattr(pipeline, 'default_handler', lambda stage: lambda exp, config: 'resumed')
        assert main(['run', str(tmp_path), '--until', 'prepare', '--no-panel']) == 0
        assert read_json(tmp_path/'state/prepare.json')['notes'] == 'resumed'
    finally:
        if child.is_alive():
            child.kill()
        child.join()


@pytest.mark.parametrize('options', [['--wait', '-1'], ['--wait', 'nan'], ['--wait', 'inf'],
                                     ['--wait', '5', '--no-wait']])
def test_stop_rejects_invalid_wait(options):
    from test_survival.cli import parser
    with pytest.raises(SystemExit):
        parser().parse_args(['stop', 'experiment', *options])
