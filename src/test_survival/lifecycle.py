"""Identify and clean up only processes and containers owned by a run."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4

from .io import now, read_json, write_json


def process_identity(pid):
    if type(pid) is not int or pid <= 1:
        return None
    if sys.platform == 'darwin':
        import ctypes
        import errno
        # Public proc_bsdinfo layout from macOS sys/proc_info.h. Unlike ps's
        # lstart column, the kernel birth timestamp includes microseconds.
        class BSDInfo(ctypes.Structure):
            _fields_ = ([(name, ctypes.c_uint32) for name in
                         ('flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid',
                          'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
                        + [('command', ctypes.c_char*16), ('name', ctypes.c_char*32)]
                        + [(name, ctypes.c_uint32) for name in
                           ('nfiles', 'pgid', 'jobc', 'tdev', 'tpgid', 'nice')]
                        + [('seconds', ctypes.c_uint64), ('microseconds', ctypes.c_uint64)])
        info = BSDInfo()
        query = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True).proc_pidinfo
        query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        query.restype = ctypes.c_int
        if query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            error = ctypes.get_errno()
            if error in (errno.ESRCH, errno.ENOENT):
                return None
            raise OSError(error, 'Could not read process identity')
        return None if info.status == 5 else f'darwin:{info.seconds}:{info.microseconds}'
    if sys.platform.startswith('linux'):
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except FileNotFoundError:
            return None
        return None if fields[0] == 'Z' else f'linux:{boot}:{fields[19]}'
    result = subprocess.run(['ps', '-o', 'lstart=,stat=', '-p', str(pid)],
                            capture_output=True, text=True, env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'})
    if not result.stdout.strip():
        if result.returncode not in (0, 1):
            raise RuntimeError('Could not read process identity')
        return None
    birth, status = result.stdout.strip().rsplit(None, 1)
    if status.startswith('Z'):
        return None
    return birth


def signal_owned(record, sig):
    pid = record['pid']
    if pid == os.getpid():
        raise RuntimeError('Refusing to signal the stopping process')
    if record.get('identity') and process_identity(pid) == record['identity']:
        try:
            os.kill(pid, sig)
            return True
        except ProcessLookupError:
            pass
    return False


def owner_experiment(exp):
    """Pool workspaces publish ownership explicitly; never infer it from parents."""
    exp = Path(exp).resolve()
    owner = read_json(exp/'state/run-owner.json')
    if owner is None:
        return exp
    root = Path(owner['experiment']).resolve()
    if root == exp or not exp.is_relative_to(root):
        raise RuntimeError('Invalid nested run owner')
    run = read_json(root/'state/run.json', {})
    if not owner.get('run_id') or run.get('run_id') != owner['run_id'] or run.get('status') != 'running':
        raise RuntimeError('Nested workspace belongs to an inactive run')
    return root


def launch(exp, command, **kwargs):
    """Retain identity even if the coordinator dies before its worker exits."""
    exp = owner_experiment(exp)
    if (exp/'state/stopping.json').exists():
        raise RuntimeError('Experiment stop cleanup is in progress')
    run = read_json(exp/'state/run.json', {})
    child = subprocess.Popen(command, **kwargs)
    if run.get('run_id'):
        record = dict(pid=child.pid, identity=process_identity(child.pid), run_id=run['run_id'])
        write_json(exp/'state/workers'/f'{uuid4().hex}.json', record)
    return child


def container_options(exp):
    """Record launch intent before Docker can create even a short-lived container."""
    exp = owner_experiment(exp)
    if (exp/'state/stopping.json').exists():
        raise RuntimeError('Experiment stop cleanup is in progress')
    run = read_json(exp/'state/run.json', {})
    if not run.get('run_id'):
        return []
    name = 'survival-'+uuid4().hex
    write_json(exp/'state/containers'/f'{name}.json', dict(name=name, run_id=run['run_id']))
    return ['--name', name, '--label', 'test-survival.run='+run['run_id']]


def freeze_workers(exp, record, pending):
    """Freeze parents before enumerating children, including separate sessions."""
    targets = {r['pid']: r for r in pending.get('processes', [])}
    targets[record['pid']] = dict(pid=record['pid'], identity=record.get('identity'))
    for path in (exp/'state/workers').glob('*.json'):
        worker = read_json(path)
        if record.get('run_id') and worker.get('run_id') == record['run_id']:
            # A long run can reuse a worker PID. Prefer the recorded instance
            # that still exists, not whichever historical file was read first.
            if worker['pid'] not in targets or (worker.get('identity') and process_identity(worker['pid']) == worker['identity']):
                targets[worker['pid']] = worker
    frozen = set()
    deadline = time.monotonic()+10
    while True:
        pending['processes'] = list(targets.values())
        write_json(exp/'state/stopping.json', pending)
        for pid, target in list(targets.items()):
            if pid not in frozen and signal_owned(target, signal.SIGSTOP):
                frozen.add(pid)
        snapshot = subprocess.run(['ps', '-axo', 'pid=,ppid='], capture_output=True, text=True, check=True)
        added = False
        parents = {pid for pid in frozen if process_identity(pid) == targets[pid]['identity']}
        for line in snapshot.stdout.splitlines():
            pid, parent = map(int, line.split())
            if parent in parents:
                identity = process_identity(pid)
                if identity and targets.get(pid, {}).get('identity') != identity:
                    targets[pid] = dict(pid=pid, identity=identity)
                    frozen.discard(pid)
                    added = True
        if not added:
            return list(targets.values())
        if time.monotonic() >= deadline:
            raise RuntimeError('Could not freeze all experiment workers')


def terminate_workers(targets):
    # All writers are frozen. Do not resume them between cleanup and lock handoff.
    for target in reversed(targets):
        signal_owned(target, signal.SIGKILL)
    deadline = time.monotonic()+10
    while any(t.get('identity') and process_identity(t['pid']) == t['identity'] for t in targets):
        if time.monotonic() >= deadline:
            raise RuntimeError('Experiment workers did not exit')
        time.sleep(.05)


def cleanup_containers(exp, record):
    run_id = record.get('run_id')
    intents = [read_json(p) for p in (exp/'state/containers').glob('*.json')]
    legacy = [p for p in (exp/'state').glob('container-*.json')
              if read_json(p, {}).get('status') == 'running']
    if not any(r.get('run_id') == run_id for r in intents) and not legacy:
        return

    def docker(*args):
        result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError('Docker cleanup failed: '+result.stderr.strip())
        return result.stdout.strip().splitlines()

    def owned():
        # Container IDs are immutable. Legacy session records provide their IDs;
        # new runs also label every one-shot, Compose and detached container.
        ids = set(docker('ps', '-aq', '--no-trunc', '--filter', 'label=test-survival.run='+run_id)) if run_id else set()
        for path in legacy:
            ident = read_json(path)['container']
            ids.update(docker('ps', '-aq', '--no-trunc', '--filter', 'id='+ident))
        return ids

    containers = owned()
    if containers:
        docker('rm', '--force', *sorted(containers))
    if owned():
        raise RuntimeError('Experiment containers remain after cleanup')
    for path in legacy:
        state = read_json(path)
        state.update(status='stopped', finished=now())
        write_json(path, state)
