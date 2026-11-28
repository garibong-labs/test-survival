"""Resolve project classpaths, coordinating the first check of each cache file."""
from contextlib import contextmanager, nullcontext
import hashlib
from pathlib import Path, PurePosixPath
import shutil
import threading
from uuid import uuid4
from ..adapters.maven_dependencies import exclusive_lock, resolution_timeout
from ..io import read_json, write_json

_GUARD = threading.Lock()
_CACHE_LOCKS = {}
_ACTIVE_JOBS = set()


@contextmanager
def _resolution(cache, job):
    path = cache.resolve()
    with _GUARD:
        lock = _CACHE_LOCKS.setdefault(path, threading.Lock())
    # After the first successful check, the container's file lock handles all writers.
    with lock:
        # Concurrent checks of the same index must each read their own worker response.
        with _GUARD:
            if job in _ACTIVE_JOBS:
                job = job.with_name(job.stem+'-'+uuid4().hex+job.suffix)
            _ACTIVE_JOBS.add(job)
        try:
            yield job
            with _GUARD:
                _CACHE_LOCKS[path] = nullcontext()
        finally:
            with _GUARD:
                _ACTIVE_JOBS.remove(job)


def _seed_cache(source, cache):
    # Another process can finish its own repair while this one waits for the file lock.
    with exclusive_lock(cache.with_suffix('.lock')):
        if cache.exists():
            return
        temporary = cache.with_name(cache.stem+'-'+uuid4().hex+'.tmp')
        try:
            shutil.copy2(source, temporary)
            temporary.replace(cache)
        finally:
            temporary.unlink(missing_ok=True)


class ClasspathCache:
    def __init__(self, exp, config, runner):
        self.exp, self.config, self.runner = Path(exp), config, runner

    def resolve(self, folder, index, *, log_root=None):
        digest = hashlib.sha256((folder / 'pom.xml').read_bytes()).hexdigest()
        # A submodule classpath names sibling jars inside this index's checkout, so it is never shared.
        module = str(PurePosixPath(self.config['project']['module']))
        enabled = (self.config['runner']['cache']['classpath'] == 'pom-sha256' and self.config['survival'].get('selection') != 'affected'
                   and module == '.')
        cache = self.exp / 'work/classpaths' / f'{digest if enabled else index}.txt'
        with _resolution(cache, (self.exp / f'work/classpath-jobs/{index}.json').resolve()) as job:
            cache.parent.mkdir(parents=True, exist_ok=True)
            reuse = self.config['runner']['reuse']['classpath_cache']
            if enabled and reuse and not cache.exists() and (Path(reuse) / cache.name).is_file():
                _seed_cache(Path(reuse) / cache.name, cache)
            # Maven paths belong to the container, not the orchestrator's filesystem.
            output = f'work/classpath-jobs/{job.stem}-result.json'
            logs = log_root or self.exp/'results/builds/logs'
            write_json(job, dict(action='classpath', logs=str(logs.relative_to(self.exp)), experiment=self.runner.path(self.exp),
                                folder=self.runner.path(folder), module=self.config['project']['module'],
                                cache=str(cache.relative_to(self.exp)), index=index, force=not enabled,
                                jvm={}, timeouts=self.config['runner']['timeouts'], output=output))
            timeout = resolution_timeout(self.config['runner']['timeouts']['build'])+600
            result = self.runner.execute(['python3', str(Path(__file__).parents[1]/'adapters/java_worker.py'), str(job)],
                                         folder, logs/f'classpath-{job.stem}.txt', timeout)
            if result['exit_code'] or not cache.exists():
                raise RuntimeError('Maven classpath resolution failed; see '+result['log'])
            attempts = read_json(self.exp/output)['attempts']
        return digest, cache, attempts
