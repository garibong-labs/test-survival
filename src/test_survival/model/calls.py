"""Coordinate native request publication with stop and retain interrupted attempts."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
from pathlib import Path

from ..io import now, read_json, write_json


@contextmanager
def admission_lock(exp):
    if exp is None:  # Standalone client without an experiment owner.
        yield
        return
    state = exp/'state'
    state.mkdir(parents=True, exist_ok=True)
    with (state/'model-calls.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def active_calls(exp, run_id):
    """Snapshot only this run's outstanding requests under the admission lock."""
    paths = []
    for marker in (exp/'state/model-calls').glob('*.json'):
        entry = read_json(marker)
        if entry and entry['run_id'] == run_id:
            ledger = exp/entry['ledger']
            if (ledger/'request.json').is_file() and not (ledger/'run.json').is_file():
                paths.append(ledger)
    return paths


def register(exp, directory, run_id):
    relative = directory.resolve().relative_to(exp).as_posix()
    marker = exp/'state/model-calls'/(sha256(relative.encode()).hexdigest()+'.json')
    entry = dict(run_id=run_id, ledger=relative)
    if read_json(marker) == entry:
        raise RuntimeError(f'Model call already registered for {relative}')
    write_json(marker, entry)
    return marker


def archive_path(exp, relative, timestamp):
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise RuntimeError('Invalid experiment-relative ledger path')
    stamp = datetime.fromisoformat(timestamp).astimezone(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S.%fZ')
    path = exp/'interrupted-ledgers'/stamp/relative
    if path != path.resolve() or exp/relative != (exp/relative).resolve():
        raise RuntimeError('Cannot move a symlinked call ledger')
    return path


def recover_archive(exp):
    """Roll back a move that never reached its index commit point."""
    journal = exp/'state/interrupted-ledger-move.json'
    entry = read_json(journal)
    if entry is None:
        return
    relative = Path(entry['original_path'])
    original = exp/relative
    moved = archive_path(exp, relative, entry['time'])
    if entry['new_path'] != moved.relative_to(exp).as_posix():
        raise RuntimeError('Interrupted ledger move has an inconsistent archive path')
    if moved.is_dir():
        history = read_json(exp/'interrupted-ledgers/index.json', [])
        if not history or history[-1] != entry:
            if original.exists():
                raise RuntimeError('Cannot restore interrupted ledger over an existing path')
            moved.rename(original)
    elif not (original/'request.json').is_file():
        raise RuntimeError('Interrupted ledger missing from both original and archive paths')
    journal.unlink()


def archive_interrupted(exp, directory):
    if directory.is_symlink():
        raise RuntimeError(f'Cannot archive a symlinked call ledger: {directory}')
    original = directory.resolve().relative_to(exp)
    timestamp = now()
    dest = archive_path(exp, original, timestamp)
    if dest.exists():
        raise RuntimeError(f'Interrupted ledger destination is already in use: {dest}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(original_path=original.as_posix(), new_path=dest.relative_to(exp).as_posix(),
                 request_sha256=sha256((directory/'request.json').read_bytes()).hexdigest(),
                 time=timestamp)
    index = exp/'interrupted-ledgers/index.json'
    history = read_json(index, [])
    journal = exp/'state/interrupted-ledger-move.json'
    write_json(journal, entry)
    directory.rename(dest)
    write_json(index, [*history, entry])
    journal.unlink()
