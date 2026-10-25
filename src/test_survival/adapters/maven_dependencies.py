"""Validate and repair project classpaths inside the Maven volume's container."""
import fcntl
from pathlib import Path
import time
from uuid import uuid4

RETRIES = 3
BACKOFF_SECONDS = 60
TRANSIENT_PATTERNS = ('429', 'Could not transfer')


def resolution_timeout(build_seconds):
    """Worst case for both dependency goals, each offline then online with bounded backoff."""
    return 2*((RETRIES+2)*build_seconds + RETRIES*BACKOFF_SECONDS)


def entries(cache):
    return [Path(p) for p in cache.read_text().strip().split(':') if p]


def valid(cache):
    return cache.is_file() and all(p.exists() for p in entries(cache))


def maven(goal, folder, logs, timeout, execute, wait=time.sleep, options=(), verify=None):
    """Try offline first, then online with bounded transfer/429 retries."""
    logs = logs / (goal.split(':')[-1]+'-'+uuid4().hex)
    attempts = []
    for number in range(RETRIES + 2):
        offline = number == 0
        log = logs / f'{number + 1}.txt'
        result = execute(['mvn', '-q', *(['-o'] if offline else ['-U']), goal, *options],
                         folder, log, timeout)
        attempts.append(result)
        if result['exit_code'] == 0 and (verify is None or verify()):
            return attempts
        if offline:
            continue
        # A successful goal whose output still names missing entries will not improve on retry.
        if result['exit_code'] == 0 or number == RETRIES + 1 or not any(p in log.read_text(errors='replace') for p in TRANSIENT_PATTERNS):
            break
        wait(BACKOFF_SECONDS)
    reason = 'produced entries missing from this container' if result['exit_code'] == 0 else 'failed'
    raise RuntimeError(f'Maven {goal} {reason}; see {log}')


def resolve(folder, cache, logs, timeout, execute, *, prefetch=False, force=False, wait=time.sleep):
    """Publish only a successful classpath whose entries exist in this container."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    attempts = []
    # Multiple worker processes may discover the same stale pom digest together.
    with cache.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if prefetch or not cache.exists():
            attempts.extend(maven('dependency:go-offline', folder, logs, timeout, execute, wait))
        if force or not valid(cache):
            temporary = cache.with_name(cache.stem+'-'+uuid4().hex+'.tmp')
            try:
                # maven() returns only once verify() accepted this file.
                attempts.extend(maven('dependency:build-classpath', folder, logs, timeout, execute, wait,
                                      ('-DincludeScope=test', '-Dmdep.outputFile='+str(temporary)),
                                      verify=lambda: valid(temporary)))
                temporary.replace(cache)
            finally:
                temporary.unlink(missing_ok=True)
    return attempts
