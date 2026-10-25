"""Stage transactions, resumability, and exclusive foreground/background runs."""
from contextlib import contextmanager, nullcontext
import fcntl
import importlib
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from .config import STAGES, stage_hash
from .io import atomic_text, now, read_json, write_json
from .progress import describe
from .lifecycle import process_identity, freeze_workers, terminate_workers, cleanup_containers


class PipelineError(RuntimeError):
    pass


def log(exp, message, config=None):
    from .panel import OUTPUT_LOCK, notify
    stage = message.partition(':')[0]
    if config is not None and stage in STAGES:
        message += ' | ' + describe(exp, stage, config)
    line = f'{now()} {message}'
    with OUTPUT_LOCK:
        print(line, flush=True)
        path = Path(exp) / 'state/log.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            stream.write(line + '\n')
    notify(exp)


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith('Z')


@contextmanager
def run_lock(exp, inherited_fd=None, *, stopping=False):
    state = Path(exp) / 'state'
    state.mkdir(parents=True, exist_ok=True)
    fd = inherited_fd if inherited_fd is not None else os.open(state / 'run.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic()+5 if stopping else 0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise PipelineError('An experiment run is already alive; refusing a second run') from exc
                time.sleep(.05)
        if not stopping and (state/'stopping.json').exists():
            raise PipelineError('Stop cleanup is incomplete; retry survival stop before resuming')
        yield fd
    finally:
        # close, not LOCK_UN: background children may share this open description.
        os.close(fd)


def invalidate(exp, start):
    for later in STAGES[STAGES.index(start)+1:]:
        path = Path(exp) / 'state' / f'{later}.json'
        record = read_json(path)
        if record and record['status'] == 'completed':
            record.update(status='pending', notes=f'Invalidated by {start}')
            write_json(path, record)


def stop(exp):
    exp = Path(exp).resolve()
    if not (exp/'state/run.json').exists():
        raise PipelineError('No running experiment recorded in state/run.json')
    with (exp/'state/stop.lock').open('a') as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PipelineError('An experiment stop is already in progress') from exc
        # If no run owns the flock, hold it while recovering stale state. If a
        # run owns it, verify its identity before installing the resume barrier.
        with (exp/'state/run.lock').open('a') as gate:
            try:
                fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                idle = True
            except BlockingIOError:
                idle = False
            return _stop(exp, idle=idle)


def _stop(exp, *, idle):
    run_path = exp/'state/run.json'
    record = read_json(run_path, {})
    pending_path = exp/'state/stopping.json'
    pending = read_json(pending_path)
    if record.get('status') != 'running' and pending is None:
        raise PipelineError('No running experiment recorded in state/run.json')
    pid = record.get('pid')
    if type(pid) is not int or pid <= 1 or pid == os.getpid():
        raise PipelineError('Refusing invalid run PID in state/run.json')
    live = process_identity(pid)
    if live and not record.get('identity'):
        raise PipelineError('Run has no saved process identity; refusing to signal an unverified PID')
    if not idle and (not live or live != record.get('identity')):
        raise PipelineError('Run lock is held by an unverified process; refusing stale-state recovery')
    # A mismatched live identity is a reused PID, not an experiment process.
    # Its signal is skipped, while stale experiment resources can be recovered.
    if pending and pending['run'].get('run_id') != record.get('run_id'):
        raise PipelineError('Run changed during stop cleanup; state retained')
    pending = pending or dict(run=record, processes=[], stages={
        stage: state.get('started') for stage in STAGES
        if (state := read_json(exp/'state'/f'{stage}.json', {})).get('status') == 'running'})
    write_json(pending_path, pending)
    try:
        targets = freeze_workers(exp, record, pending)
        terminate_workers(targets)
        # Keep a durable resume barrier until both host workers and containers
        # are confirmed gone, even after the coordinator releases its flock.
        with nullcontext() if idle else run_lock(exp, stopping=True):
            current = read_json(run_path, {})
            if (current.get('pid'), current.get('run_id')) != (pid, record.get('run_id')):
                raise PipelineError('Run changed while stopping; new run state retained')
            cleanup_containers(exp, record)
            finished = now()
            for stage in STAGES:
                path = exp/'state'/f'{stage}.json'
                state = read_json(path, {})
                if state.get('status') == 'running' or (stage in pending.get('stages', {}) and state.get('started') == pending['stages'][stage]):
                    state.update(status='failed', finished=finished, notes='stopped by user')
                    write_json(path, state)
            current.update(status='failed', finished=finished, notes='stopped by user')
            write_json(run_path, current)
            pending_path.unlink()
            log(exp, f'stopped process {pid}: stopped by user')
    except Exception as exc:
        raise PipelineError(f'Stop cleanup incomplete; resume blocked. Retry survival stop: {exc}') from exc
    return pid


def default_handler(stage):
    return importlib.import_module(f'test_survival.stages.{stage}').run


def run(exp, config, *, until=None, stage=None, force=False, group=None, handlers=None, locked=False, no_panel=False):
    exp = Path(exp).resolve()
    if not locked:
        with run_lock(exp):
            return run(exp, config, until=until, stage=stage, force=force, group=group, handlers=handlers, locked=True, no_panel=no_panel)
    if stage and until:
        raise PipelineError('--stage and --until are mutually exclusive')
    if group and stage != 'generate':
        raise PipelineError('--group requires --stage generate')
    if group and group not in [g['name'] for g in config['generators']]:
        raise PipelineError('--group must name a configured generator')
    selected = (stage,) if stage else STAGES[:STAGES.index(until)+1] if until else STAGES
    if stage:
        for earlier in STAGES[:STAGES.index(stage)]:
            previous = read_json(exp / 'state' / f'{earlier}.json', {})
            if previous.get('status') != 'completed' or previous.get('config_hash') != stage_hash(config, earlier):
                raise PipelineError(f'{stage} requires completed, current stage {earlier}')
    from .panel import LiveProgress
    with LiveProgress(exp, config, disabled=no_panel):
        return _run(exp, config, selected=selected, force=force, group=group, handlers=handlers)


def _run(exp, config, *, selected, force=False, group=None, handlers=None):
    run_path = exp / 'state/run.json'
    record = read_json(run_path, {})
    log_path = record.get('log') if record.get('pid') == os.getpid() and record.get('status') == 'running' else str(exp / 'state/log.txt')
    identity = process_identity(os.getpid())
    if identity is None:
        raise PipelineError('Could not establish the run process identity')
    same_run = record.get('pid') == os.getpid() and record.get('identity') == identity and record.get('status') == 'running'
    run_id = (record.get('run_id') if same_run else None) or uuid4().hex
    record.update(pid=os.getpid(), identity=identity, run_id=run_id, log=log_path, status='running', started=now(), finished=None)
    write_json(run_path, record)
    completed = []
    try:
        for current in selected:
            path = exp / 'state' / f'{current}.json'
            digest = stage_hash(config, current)
            previous = read_json(path, {})
            if not force and previous.get('status') == 'completed' and previous.get('config_hash') == digest:
                log(exp, f'{current}: already completed; skipped', config)
                continue
            invalidate(exp, current)
            state = dict(status='running', config_hash=digest, started=now(), finished=None, notes='')
            if current == 'generate' and group:
                state['group'] = group
            write_json(path, state)
            log(exp, f'{current}: started', config)
            try:
                handler = handlers[current] if handlers is not None else default_handler(current)
                summary = handler(exp, config, group=group, force=force) if current == 'generate' and handlers is None else handler(exp, config, group=group) if group else handler(exp, config)
            except BaseException as exc:
                # Always leave a concrete diagnostic, including failures before
                # the stage has created its first evidence file. Keep it off stdout.
                error_path = exp/'state'/f'{current}-error.txt'
                atomic_text(error_path, f'{type(exc).__name__}: {exc}\n')
                state.update(status='failed', finished=now(), notes=f'{type(exc).__name__}; see {error_path.relative_to(exp)}')
                write_json(path, state)
                log(exp, f'{current}: failed ({type(exc).__name__}); see {error_path}', config)
                raise
            state.update(status='completed', finished=now(), notes=summary or f'{current} completed')
            if current == 'generate' and group and handlers is None:
                hashes = dict(previous.get('group_hashes', {}))
                hashes[group] = stage_hash(config, 'generate', group)
                state['group_hashes'] = hashes
                if any(hashes.get(g['name']) != stage_hash(config, 'generate', g['name']) for g in config['generators']):
                    state['status'] = 'pending'
            write_json(path, state)
            log(exp, f'{current}: {state["notes"]}', config)
            completed.append(current)
        record.update(status='completed', finished=now())
        return completed
    except BaseException:
        record.update(status='failed', finished=now())
        raise
    finally:
        write_json(run_path, record)


def background(exp, *, until=None, stage=None, force=False, group=None):
    exp = Path(exp).resolve()
    with run_lock(exp) as fd:
        ready_read, ready_write = os.pipe()
        command = [sys.executable, '-m', 'test_survival.cli', 'run', str(exp), '--lock-fd', str(fd), '--ready-fd', str(ready_read)]
        for option, value in [('--until', until), ('--stage', stage), ('--group', group)]:
            if value:
                command.extend([option, value])
        if force:
            command.append('--force')
        path = exp / 'state/background.log'
        with path.open('a') as stream:
            child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     start_new_session=True, pass_fds=(fd, ready_read))
        os.close(ready_read)
        try:
            write_json(exp / 'state/run.json', dict(pid=child.pid, identity=process_identity(child.pid), run_id=uuid4().hex, log=str(path), status='running', started=now()))
            os.write(ready_write, b'1')
        finally:
            os.close(ready_write)
        return child.pid, path


def status(exp, config):
    exp = Path(exp)
    rows = []
    for stage in STAGES:
        state = read_json(exp / 'state' / f'{stage}.json', {})
        label = state.get('status', 'pending')
        if label == 'completed' and state.get('config_hash') != stage_hash(config, stage):
            label = 'stale'
        rows.append(f'{stage:10} {label} | {describe(exp, stage, config)}')
    active = read_json(exp / 'state/run.json', {})
    bg = read_json(exp / 'state/background.json', {})
    if active.get('status') == 'running':
        rows.append(f'Process {active.get("pid")}: {"alive" if process_alive(active.get("pid")) else "stopped"}')
    elif bg and process_alive(bg.get('pid')):
        rows.append(f'Process {bg["pid"]}: alive')
    denominators = read_json(exp / 'results/denominators.json')
    if denominators is not None:
        rows.append('Denominators: ' + ', '.join(f'{g}={n}' for g, n in denominators.items()))
    survival = exp / 'results/survival.csv'
    if survival.exists():
        from .io import read_csv
        records = read_csv(survival)
        if records:
            rows.append('Survivors: ' + ', '.join(f'{k[:-6]}={v}' for k, v in records[-1].items() if k.endswith('_alive')))
    path = exp / 'state/log.txt'
    if path.exists():
        lines = path.read_text().splitlines()
        if lines:
            rows.append('Last log: ' + lines[-1])
    return '\n'.join(rows)
