"""Stage transactions, resumability, and exclusive foreground/background runs."""
from contextlib import contextmanager
import fcntl
import importlib
import os
from pathlib import Path
import subprocess
import sys

from .config import STAGES, stage_hash
from .io import now, read_json, write_json


class PipelineError(RuntimeError):
    pass


def log(exp, message):
    line = f'{now()} {message}'
    print(line, flush=True)
    path = Path(exp) / 'state/log.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        stream.write(line + '\n')


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def run_lock(exp, inherited_fd=None):
    state = Path(exp) / 'state'
    state.mkdir(parents=True, exist_ok=True)
    fd = inherited_fd if inherited_fd is not None else os.open(state / 'run.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PipelineError('An experiment run is already alive; refusing a second run') from exc
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


def default_handler(stage):
    return importlib.import_module(f'test_survival.stages.{stage}').run


def run(exp, config, *, until=None, stage=None, force=False, group=None, handlers=None, locked=False):
    exp = Path(exp).resolve()
    if not locked:
        with run_lock(exp):
            return run(exp, config, until=until, stage=stage, force=force, group=group, handlers=handlers, locked=True)
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
    run_path = exp / 'state/run.json'
    record = read_json(run_path, {})
    record.update(pid=os.getpid(), log=record.get('log', str(exp / 'state/log.txt')), status='running', started=now())
    write_json(run_path, record)
    completed = []
    try:
        for current in selected:
            path = exp / 'state' / f'{current}.json'
            digest = stage_hash(config, current)
            previous = read_json(path, {})
            if not force and previous.get('status') == 'completed' and previous.get('config_hash') == digest:
                log(exp, f'{current}: already completed; skipped')
                continue
            invalidate(exp, current)
            state = dict(status='running', config_hash=digest, started=now(), finished=None, notes='')
            write_json(path, state)
            log(exp, f'{current}: started')
            try:
                handler = handlers[current] if handlers is not None else default_handler(current)
                summary = handler(exp, config, group=group) if group else handler(exp, config)
            except BaseException as exc:
                # Tracebacks and arbitrary exception strings may include request secrets.
                state.update(status='failed', finished=now(), notes=f'{type(exc).__name__}; see stage evidence')
                write_json(path, state)
                log(exp, f'{current}: failed ({type(exc).__name__})')
                raise
            state.update(status='completed', finished=now(), notes=summary or f'{current} completed')
            write_json(path, state)
            log(exp, f'{current}: {state["notes"]}')
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
            write_json(exp / 'state/run.json', dict(pid=child.pid, log=str(path), status='running', started=now()))
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
        rows.append(f'{stage:10} {label}')
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
