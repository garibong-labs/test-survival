"""Exclusive, reusable working copies of frozen resources for container JVMs."""
from contextlib import contextmanager
import fcntl
from itertools import chain, count
import json
from pathlib import Path
import shutil
import sys


@contextmanager
def runtime_slot(pool):
    """Allocate at most one working tree per concurrently active launcher."""
    pool.mkdir(parents=True, exist_ok=True)
    for index in count():
        slot = pool/str(index)
        slot.mkdir(exist_ok=True)
        with (slot/'lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            cwd = slot/'cwd'
            cwd.mkdir(exist_ok=True)
            yield cwd
            return


def signature(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_ctime_ns]


def reset_runtime(source, cwd):
    """Restore the lease before use, including dirt left by interrupted jobs."""
    ledger = cwd.parent/'files.json'
    previous = json.loads(ledger.read_text()) if ledger.exists() else {}
    files = {}
    directories = {'src', 'src/test'}
    if source.is_dir():
        for folder in source.iterdir():
            if folder.name == 'java' or not folder.is_dir():
                continue
            for path in chain((folder,), folder.rglob('*')):
                relative = 'src/test/'+path.relative_to(source).as_posix()
                if path.is_dir():
                    directories.add(relative)
                elif path.is_file():
                    files[relative] = path
    # Never traverse symlinks left by a test, even when a matching name is wanted.
    def prune(parent):
        for path in parent.iterdir():
            name = path.relative_to(cwd).as_posix()
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                if name in directories:
                    prune(path)
                else:
                    shutil.rmtree(path)
            elif name not in files or not path.is_file():
                path.unlink()
    prune(cwd)
    for name in sorted(directories):
        (cwd/name).mkdir(exist_ok=True)
    current = {}
    for name, path in files.items():
        target = cwd/name
        original = signature(path)
        staged = signature(target) if target.exists() else None
        if staged is None or previous.get(name) != [original, staged]:
            # Unlink first: a test can have replaced the path with a hard link.
            target.unlink(missing_ok=True)
            shutil.copy2(path, target)
            staged = signature(target)
        current[name] = [original, staged]
    temporary = ledger.with_suffix('.tmp')
    temporary.write_text(json.dumps(current))
    temporary.replace(ledger)


if __name__ == '__main__':
    reset_runtime(Path(sys.argv[1]), Path.cwd())
