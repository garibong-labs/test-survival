"""Validate and repair project classpaths inside the Maven volume's container."""
import fcntl
from pathlib import Path, PurePosixPath
import time
from uuid import uuid4

RETRIES = 3
BACKOFF_SECONDS = 60
TRANSIENT_PATTERNS = ('429', 'Could not transfer')
# Goals that resolve the module's own dependencies. In a reactor build a sibling
# module's SNAPSHOT exists only after that sibling is packaged in the same build.
REACTOR_ARTIFACT_GOALS = ('dependency:build-classpath', 'dependency:list', 'dependency:copy-dependencies')


def reactor(folder, module):
    """Working directory and selector for one module of a multi-module project.

    A submodule depends on sibling modules whose SNAPSHOT versions exist in no
    repository. Maven resolves them only from the reactor, so commands run at the
    checkout root with `-pl <module> -am`. A root module needs nothing extra.
    """
    module = str(PurePosixPath(module or '.'))
    if module == '.':
        return Path(folder), []
    root = Path(folder)
    for _ in PurePosixPath(module).parts:
        root = root.parent
    return root, ['-pl', module, '-am']


def command(goal, folder, module='.', *, offline=False, refresh=False, options=()):
    """Maven command and working directory; artifact goals package the reactor first."""
    cwd, selector = reactor(folder, module)
    # maven.test.skip also skips test compilation: a checkout whose own tests no longer
    # compile still has usable production code and dependencies.
    build = ['-Dmaven.test.skip=true', 'package'] if selector and goal in REACTOR_ARTIFACT_GOALS else []
    flags = ['-o'] if offline else ['-U'] if refresh else []
    return ['mvn', '-q', *flags, *selector, *build, goal, *options], cwd


def resolution_timeout(build_seconds):
    """Worst case for both dependency goals, each offline then online with bounded backoff."""
    return 2*((RETRIES+2)*build_seconds + RETRIES*BACKOFF_SECONDS)


def entries(cache):
    return [Path(p) for p in cache.read_text().strip().split(':') if p]


def valid(cache):
    return cache.is_file() and all(p.exists() for p in entries(cache))


def maven(goal, folder, logs, timeout, execute, wait=time.sleep, options=(), verify=None, module='.'):
    """Try offline first, then online with bounded transfer/429 retries."""
    logs = logs / (goal.split(':')[-1]+'-'+uuid4().hex)
    attempts = []
    for number in range(RETRIES + 2):
        offline = number == 0
        log = logs / f'{number + 1}.txt'
        args, cwd = command(goal, folder, module, offline=offline, refresh=not offline, options=options)
        result = execute(args, cwd, log, timeout)
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


def resolve(folder, cache, logs, timeout, execute, *, prefetch=False, force=False, wait=time.sleep, module='.'):
    """Publish only a successful classpath whose entries exist in this container."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    attempts = []
    # Multiple worker processes may discover the same stale pom digest together.
    with cache.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if prefetch or not cache.exists():
            attempts.extend(maven('dependency:go-offline', folder, logs, timeout, execute, wait, module=module))
        if force or not valid(cache):
            temporary = cache.with_name(cache.stem+'-'+uuid4().hex+'.tmp')
            try:
                # maven() returns only once verify() accepted this file.
                attempts.extend(maven('dependency:build-classpath', folder, logs, timeout, execute, wait,
                                      ('-DincludeScope=test', '-Dmdep.outputFile='+str(temporary)),
                                      verify=lambda: valid(temporary), module=module))
                temporary.replace(cache)
            finally:
                temporary.unlink(missing_ok=True)
    return attempts
