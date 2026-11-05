"""Success-only workspace release. Callers own the experiment run lock.

A completed result is authoritative; work is expendable only after its consumers
validate it. Published locks, model output and unknown paths are never removed.
"""
import os
from pathlib import Path
import shutil
import stat

from .io import read_json


def remove_owned(exp, relative):
    """Delete beneath an open experiment descriptor without following parent links."""
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts or relative.parts[:1] != ('work',):
        raise ValueError('Cleanup requires an experiment-relative work path')
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError('Safe workspace deletion is unavailable on this platform')
    fd = os.open(exp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return
            os.close(fd); fd = child
        name = relative.name
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError('Refusing symlink workspace: '+str(relative))
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


def release_checkout(exp, config, index, *, final=False):
    """Release a verified index after all consumers and coordinator checks succeed."""
    exp = Path(exp)
    if getattr(config, 'keep_work', False) or (int(index) == 0 and not final):
        return
    record = read_json(exp/f'results/builds/{index}.json', {})
    identity = read_json(exp/f'results/workspace-identities/{index}.json', {})
    # Old experiments lacking a trustworthy dependency identity are readable but
    # are not retroactively pruned. Never lose a failed build's working evidence.
    if record.get('build') != 'ok' or not identity or identity.get('sha') != record.get('sha'):
        return
    remove_owned(exp, f'work/checkouts/{index}')
    remove_owned(exp, f'work/checkouts/{index}.json')


def finish_checkouts(exp, config):
    if getattr(config, 'keep_work', False):
        return
    root = Path(exp)/'work/checkouts'
    if root.is_symlink():
        raise RuntimeError('Refusing symlink checkouts')
    if not root.exists():
        return
    for path in sorted(root.glob('*.json')):
        if path.stem.isdigit():
            release_checkout(exp, config, path.stem, final=True)
    if root.exists() and not any(root.iterdir()):
        remove_owned(exp, 'work/checkouts')
