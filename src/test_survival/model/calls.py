"""Coordinate native request publication with stop and retain interrupted attempts."""
from contextlib import contextmanager
import fcntl
from hashlib import sha256

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


def recover_archive(exp):
    """Roll back a move that never reached its index commit point."""
    journal = exp/'state/interrupted-ledger-move.json'
    entry = read_json(journal)
    if entry is None:
        return
    original = exp/entry['original_path']
    moved = exp/entry['new_path']
    if moved.is_dir():
        history = read_json(exp/'interrupted-ledgers/index.json', [])
        if not history or history[-1] != entry:
            moved.rename(original)
    elif not (original/'request.json').is_file():
        raise RuntimeError('Interrupted ledger missing from both original and archive paths')
    journal.unlink()


def archive_interrupted(exp, directory):
    if directory.is_symlink():
        raise RuntimeError(f'Cannot archive a symlinked call ledger: {directory}')
    original = directory.resolve().relative_to(exp)
    timestamp = now()
    stamp = timestamp.replace('+00:00', 'Z').replace(':', '-')
    dest = exp/'interrupted-ledgers'/stamp/original
    if dest.resolve() != dest or dest.exists():
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
