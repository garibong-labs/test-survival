"""One-pass, verified experiment archives and conservative local pruning."""
from contextlib import contextmanager, ExitStack
import csv
import ctypes
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
import zlib
from uuid import uuid4

from .io import read_json


class ArchiveError(ValueError):
    pass


REGENERABLE = frozenset({
    'checkouts', 'node_modules', '.venv', 'target', 'dist', '__pycache__',
    '.gradle', '.pytest_cache', '.pnpm-store', 'compile-cache',
})
INDEX_FIELDS = ('archived_utc', 'fork', 'exp_dir', 'zip_relative_path',
                'files', 'bytes', 'sha256', 'branch', 'head_sha')
CHUNK_SIZE = 1024 * 1024


def git(repo, *args, optional=False):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if result.returncode and not optional:
        raise ArchiveError(f'Cannot read fork git metadata: {result.stderr.decode(errors="replace").strip()}')
    return result.stdout.decode('utf-8', errors='surrogateescape').strip() if not result.returncode else None


@contextmanager
def directory_fd(path, *, parent_fd=None):
    fd = (os.dup(path) if isinstance(path, int) else
          os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd))
    try:
        yield fd
    finally:
        os.close(fd)


def regular_bytes(name, parent_fd, *, missing=None, strict=False):
    """Read only an opened regular file, never a link target or a blocking FIFO."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    except FileNotFoundError:
        return missing
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            if strict:
                raise ArchiveError(f'Invalid symlinked {name}; expected a regular file') from exc
            return None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            if strict:
                raise ArchiveError(f'Invalid {name}; expected a regular file')
            return None
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def status_file_bytes(exp, *, include_config=True):
    with directory_fd(exp) as root_fd, ExitStack() as stack:
        if include_config:
            data = regular_bytes('experiment.yaml', root_fd)
            if data is not None:
                yield 'experiment.yaml', data
        try:
            state_fd = stack.enter_context(directory_fd('state', parent_fd=root_fd))
        except FileNotFoundError:
            return
        for name in sorted(os.listdir(state_fd)):
            if name.endswith('.json'):
                data = regular_bytes(name, state_fd)
                if data is not None:
                    yield 'state/'+name, data


def status_files(exp):
    """Identify restored/resumed state without changing execution provenance."""
    return {name: hashlib.sha256(data).hexdigest() for name, data in status_file_bytes(exp)}


def archived_status(exp):
    record = read_json(Path(exp)/'ARCHIVED.json')
    if not record:
        return None
    retained = record.get('retained_status_files', {})
    if any(retained.get(name) != digest for name, digest in status_files(exp).items()):
        return None  # Restoring evidence, initializing, or resuming supersedes the marker.
    return f'archived to {record["destination"]}'


@contextmanager
def idle_experiment(exp, *, dry_run):
    """Current baseline and policy runs share run.lock; also honor legacy locks."""
    state = exp/'state'
    if state.is_symlink():
        raise ArchiveError('Refusing a symlinked state directory')
    with ExitStack() as stack:
        missing = object()
        try:
            with directory_fd(state) as state_fd:
                data = regular_bytes('run-owner.json', state_fd, missing=missing, strict=True)
        except FileNotFoundError:
            data = missing
        owner = json.loads(data) if data is not missing else missing
        owner_locks = set()
        if owner is not missing:
            if (not isinstance(owner, dict) or not isinstance(owner.get('experiment'), str)
                    or not Path(owner['experiment']).is_absolute()):
                raise ArchiveError('Invalid state/run-owner.json; archive the owning experiment instead')
            parent = Path(owner['experiment']).expanduser().resolve()
            if parent == exp or not exp.is_relative_to(parent) or not (parent/'state/run.lock').is_file():
                raise ArchiveError('Cannot guard the policy workspace owner; archive the owning experiment instead')
            # Parent first, and never create or change anything outside EXP_DIR.
            # Checking the run ID alone is insufficient: a new run also owns this workspace.
            owner_locks = stack.enter_context(idle_experiment(parent, dry_run=True))
        locks = set(state.glob('*.lock'))
        locks.update(p for p in (state/'policy/run.lock', exp/'results/policy/state/run.lock') if p.exists())
        if not dry_run:
            state.mkdir(exist_ok=True)
            locks.add(state/'run.lock')
        for path in sorted(locks):
            if (path.is_symlink() or not path.resolve().is_relative_to(exp)
                    or any(p.is_symlink() for p in path.parents if p.is_relative_to(exp))):
                raise ArchiveError(f'Refusing a symlinked run lock: {path}')
            stream = stack.enter_context(path.open('rb' if dry_run else 'a+b'))
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ArchiveError(f'Experiment is running or locked: {path}') from exc
        for path in (state/'run.json', state/'policy-run.json', state/'policy/run.json',
                     exp/'results/policy/state/run.json'):
            record = read_json(path, {})
            if record.get('status') in ('running', 'starting', 'stopping'):
                raise ArchiveError(f'Experiment is running: {path} (status={record["status"]})')
        if (state/'stopping.json').exists():
            raise ArchiveError('Experiment stop cleanup is incomplete')
        yield locks | owner_locks


def excluded(relative, is_dir, include_work):
    # Paid model output is evidence even when a generator uses a cache-like name.
    if relative.parts[0] == 'generated':
        return False
    if relative.parts[:3] == ('results', 'policy', 'pools'):
        # Group/index names identify evidence; e.g. a group named "target" is
        # not a Maven build directory. Pool-generated output has the same value
        # as the top-level generated tree, regardless of names inside it.
        if is_dir and len(relative.parts) in (4, 5):
            return False
        if len(relative.parts) >= 6 and relative.parts[5] == 'generated':
            return False
    if not is_dir:
        return relative.name == '.DS_Store'
    if relative.name in REGENERABLE:
        return True
    return not include_work and (relative.parts == ('work',) or (
        relative.parts[:3] == ('results', 'policy', 'pools')
        and len(relative.parts) >= 5 and relative.name == 'work'))


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def excluded_totals(path):
    """Dry-run accounting only; the archive walker never enters excluded trees."""
    count = size = 0
    with os.scandir(path) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                sub_count, sub_size = excluded_totals(Path(entry.path))
                count += sub_count
                size += sub_size
            else:
                count += 1
                size += info.st_size
    return count, size


def walk(exp, include_work, totals, *, count_excluded=False):
    """Yield included entries, pruning regenerable directories before recursion."""
    def visit(directory):
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = Path(entry.path)
                relative = path.relative_to(exp)
                row = totals.setdefault(relative.parts[0], dict(
                    included_files=0, included_bytes=0, excluded_files=0, excluded_bytes=0,
                    included_directories=0, excluded_directories=0))
                info = entry.stat(follow_symlinks=False)
                is_dir = stat.S_ISDIR(info.st_mode)
                if excluded(relative, is_dir, include_work):
                    if is_dir:
                        row['excluded_directories'] += 1
                        if count_excluded:
                            count, size = excluded_totals(path)
                            if row['excluded_files'] is not None:
                                row['excluded_files'] += count
                                row['excluded_bytes'] += size
                        else:
                            row['excluded_files'] = row['excluded_bytes'] = None
                    elif row['excluded_files'] is not None:
                        row['excluded_files'] += 1
                        row['excluded_bytes'] += info.st_size
                elif is_dir:
                    row['included_directories'] += 1
                    yield path, info
                    yield from visit(path)
                else:
                    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                        raise ArchiveError(f'Unsupported special file: {path}')
                    row['included_files'] += 1
                    row['included_bytes'] += info.st_size
                    yield path, info
    yield from visit(exp)


def tracked_paths(repo, exp):
    relative = exp.relative_to(repo).as_posix()
    # -z preserves whitespace/newlines in names; literal pathspec avoids glob magic.
    result = subprocess.run(['git', '-C', str(repo), 'ls-files', '-z', '--', f':(literal){relative}'],
                            capture_output=True, check=True)
    return {repo/os.fsdecode(name) for name in result.stdout.split(b'\0') if name}


def print_plan(exp, totals, *, prune, tracked, locks):
    print(f'Dry run: {exp}')
    print('entry\tincluded files\tincluded bytes\texcluded files\texcluded bytes\taction')
    for name, row in sorted(totals.items()):
        label = 'included' if row['included_files'] or row['included_directories'] else 'excluded as regenerable'
        if row['included_files'] and (row['excluded_directories'] or row['excluded_files']):
            label += '; regenerable entries excluded'
        if prune:
            label += '; prune untracked files (keep git-tracked files and run locks)'
        print(f'{name}\t{row["included_files"]}\t{row["included_bytes"]}\t'
              f'{row["excluded_files"]}\t{row["excluded_bytes"]}\t{label}')
    print(f'Total included: {sum(r["included_files"] for r in totals.values())} files, '
          f'{sum(r["included_bytes"] for r in totals.values())} bytes')
    print(f'Total excluded: {sum(r["excluded_files"] for r in totals.values())} files, '
          f'{sum(r["excluded_bytes"] for r in totals.values())} bytes')
    if prune:
        print(f'Git-tracked paths preserved: {len(tracked)}')
        print(f'Existing run locks preserved: {sum(p.is_relative_to(exp) for p in locks)}')
    print('No files written or deleted.')


def manifest_for(exp, repo, include_work):
    return dict(
        format_version=1,
        tool_commit=git(Path(__file__).resolve().parent, 'rev-parse', 'HEAD', optional=True),
        archived_utc=datetime.now(timezone.utc).isoformat(), exp_dir=str(exp), fork=repo.name,
        remote_url=git(repo, 'remote', 'get-url', 'origin', optional=True),
        branch=git(repo, 'symbolic-ref', '--short', '-q', 'HEAD', optional=True),
        head_sha=git(repo, 'rev-parse', 'HEAD'),
        dirty=bool(git(repo, 'status', '--porcelain', '--untracked-files=normal')),
        stage_states={Path(name).name: json.loads(data)
                      for name, data in status_file_bytes(exp, include_config=False)},
        exclusion_rules=dict(directories=sorted(REGENERABLE), files=['.DS_Store'],
                             work=[] if include_work else ['work/', 'results/policy/pools/**/work/'],
                             protected=['generated/', 'results/policy/pools/*/',
                                        'results/policy/pools/*/*/', 'results/policy/pools/*/*/generated/']),
        excluded_counts='null means an excluded directory was not traversed',
    )


def write_zip(local, exp, repo, manifest, include_work):
    totals, snapshots = {}, {}
    with zipfile.ZipFile(local, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
        for path, info in walk(exp, include_work, totals):
            snapshots[path] = fingerprint(info)
            name = path.relative_to(repo).as_posix()
            if stat.S_ISDIR(info.st_mode):
                bundle.write(path, name)
            elif stat.S_ISLNK(info.st_mode):
                member = zipfile.ZipInfo(name)
                member.create_system = 3
                member.external_attr = info.st_mode << 16
                member.compress_type = zipfile.ZIP_DEFLATED
                bundle.writestr(member, os.fsencode(os.readlink(path)))
            else:
                # Do not follow a symlink substituted after the walk's lstat.
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as source:
                    if fingerprint(os.fstat(source.fileno())) != snapshots[path]:
                        raise ArchiveError(f'File changed during archive: {path}')
                    member = zipfile.ZipInfo.from_file(path, name)
                    member.compress_type = zipfile.ZIP_DEFLATED
                    with bundle.open(member, 'w', force_zip64=True) as target:
                        while chunk := source.read(CHUNK_SIZE):
                            target.write(chunk)
            if fingerprint(path.lstat()) != snapshots[path]:
                raise ArchiveError(f'File changed during archive: {path}')
        manifest.update(included_files=sum(r['included_files'] for r in totals.values()),
                        included_bytes=sum(r['included_bytes'] for r in totals.values()),
                        included_directories=sum(r['included_directories'] for r in totals.values()),
                        top_level_entries=totals)
        data = (json.dumps(manifest, indent=2, ensure_ascii=True) + '\n').encode()
        bundle.writestr('ARCHIVE-MANIFEST.json', data)
    with zipfile.ZipFile(local) as bundle:
        if bundle.testzip() is not None:
            raise ArchiveError('Local zip CRC verification failed')
        members = bundle.infolist()
        if (len(members) != manifest['included_files'] + manifest['included_directories'] + 1 or
                sum(m.file_size for m in members) != manifest['included_bytes'] + len(data)):
            raise ArchiveError('Local zip file count or uncompressed size verification failed')
    return snapshots


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def content_index(exp, repo, include_work):
    """Member name -> (kind, permission bits, size, CRC-32) of what write_zip would store now."""
    entries, snapshots = {}, {}
    for path, info in walk(exp, include_work, {}):
        snapshots[path] = fingerprint(info)
        name = path.relative_to(repo).as_posix()
        if stat.S_ISDIR(info.st_mode):
            entries[name+'/'] = ('dir', stat.S_IMODE(info.st_mode), 0, 0)
        elif stat.S_ISLNK(info.st_mode):
            target = os.fsencode(os.readlink(path))
            entries[name] = ('link', stat.S_IMODE(info.st_mode), len(target), zlib.crc32(target))
        else:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            crc = size = 0
            with os.fdopen(fd, 'rb') as source:
                while chunk := source.read(CHUNK_SIZE):
                    crc = zlib.crc32(chunk, crc)
                    size += len(chunk)
            entries[name] = ('file', stat.S_IMODE(info.st_mode), size, crc)
        if fingerprint(path.lstat()) != snapshots[path]:
            raise ArchiveError(f'File changed while comparing with the existing archive: {path}')
    return entries, snapshots


def reusable_archive(dest, repo, exp, include_work, exclusion_rules):
    """Latest indexed archive of EXP_DIR whose stored content equals the current tree.

    Pruning after an earlier plain archive would otherwise write a second,
    identical ZIP. Reuse requires the indexed SHA-256 to match both the sidecar
    and the destination file, the same exclusion rules, and identical member
    names, sizes and CRC-32 values (the archive manifest member aside).
    Returns (final, sha256, snapshots) or None.
    """
    index = dest/'archive-index.csv'
    if not index.exists():
        return None
    with index.open(newline='', encoding='utf-8') as stream:
        rows = [r for r in csv.DictReader(stream) if r.get('exp_dir') == str(exp)]
    current = None
    for row in reversed(rows):
        relative = row.get('zip_relative_path') or ''
        if not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
            continue
        final = dest/relative
        sidecar = final.with_suffix('.manifest.json')
        if final.is_symlink() or sidecar.is_symlink() or not final.is_file() or not sidecar.is_file():
            continue
        # Only an archive that stays in the destination and outside EXP_DIR is
        # a backup; reusing one inside the experiment would prune it too.
        resolved = final.resolve()
        if (not resolved.is_relative_to(dest) or resolved.is_relative_to(exp)
                or not sidecar.resolve().is_relative_to(dest) or sidecar.resolve().is_relative_to(exp)):
            continue
        try:
            recorded = json.loads(sidecar.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(recorded, dict) or not row.get('sha256'):
            continue
        if recorded.get('sha256') != row['sha256'] or recorded.get('exclusion_rules') != exclusion_rules:
            continue
        def kind(member):
            if stat.S_ISLNK(member.external_attr >> 16):
                return 'link'
            return 'dir' if member.is_dir() else 'file'
        try:
            with zipfile.ZipFile(resolved) as bundle:
                stored = {m.filename: (kind(m), stat.S_IMODE(m.external_attr >> 16), m.file_size, m.CRC)
                          for m in bundle.infolist() if m.filename != 'ARCHIVE-MANIFEST.json'}
        except (OSError, zipfile.BadZipFile, UnicodeDecodeError, ValueError):
            # A damaged central directory (including undecodable names) is not reusable.
            continue
        if current is None:
            current = content_index(exp, repo, include_work)
        if stored != current[0]:
            continue
        if sha256(resolved) != row['sha256']:
            continue
        # Size and CRC-32 only pre-filter; equal CRCs do not prove equal bytes.
        # Pruning deletes the local copy, so compare every member's bytes.
        if not same_bytes(resolved, repo, current[0], current[1]):
            continue
        return resolved, row['sha256'], current[1]
    return None


def same_bytes(final, repo, entries, snapshots):
    try:
        with zipfile.ZipFile(final) as bundle:
            for name, (kind, _, size, _) in entries.items():
                if kind == 'dir':
                    continue
                path = repo/name
                if kind == 'link':
                    if bundle.read(name) != os.fsencode(os.readlink(path)):
                        return False
                    continue
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as local, bundle.open(name) as stored:
                    while True:
                        mine = local.read(CHUNK_SIZE)
                        theirs = stored.read(len(mine) if mine else 1)
                        if mine != theirs:
                            return False
                        if not mine:
                            break
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, ValueError, KeyError):
        return False
    for path, recorded in snapshots.items():
        if fingerprint(path.lstat()) != recorded:
            raise ArchiveError(f'File changed while comparing with the existing archive: {path}')
    return True


@contextmanager
def destination_slot(dest, fork, exp_name, date):
    folder = dest/fork
    if not folder.resolve().is_relative_to(dest):
        raise ArchiveError(f'Fork destination escapes the destination root: {folder}')
    folder.mkdir(exist_ok=True)
    number = 1
    while True:
        suffix = '' if number == 1 else f'-{number}'
        final = folder/f'{fork}__{exp_name}__{date}{suffix}.zip'
        partial = final.with_name(final.name+'.partial')
        sidecar = final.with_suffix('.manifest.json')
        if final.exists() or final.is_symlink() or sidecar.exists() or sidecar.is_symlink():
            number += 1
            continue
        try:
            stream = partial.open('xb')
        except FileExistsError:
            number += 1
            continue
        try:
            # The .partial reservation serializes other archive writers choosing this name.
            if final.exists() or sidecar.exists():
                stream.close()
                partial.unlink()
                number += 1
                continue
            yield final, partial, sidecar, stream
            return
        finally:
            stream.close()
            partial.unlink(missing_ok=True)


def rename_exclusive(source, destination):
    """Publish without clobbering even a name created after our reservation.

    pathlib.rename replaces existing files on Unix. Use the native no-replace
    operations on the macOS host and Linux instead, failing closed elsewhere.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(source), os.fsencode(destination), 0x00000004)  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        args = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise ArchiveError('This platform does not support exclusive archive renaming')
    rename.restype = ctypes.c_int
    if rename(*args):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def copy_verified(local, partial, final, stream):
    expected = sha256(local)
    with local.open('rb') as source:
        while chunk := source.read(CHUNK_SIZE):
            stream.write(chunk)
    stream.flush()
    os.fsync(stream.fileno())
    stream.close()
    if final.exists() or final.is_symlink():
        raise ArchiveError(f'Destination already exists: {final}')
    rename_exclusive(partial, final)
    if sha256(final) != expected:
        raise ArchiveError(f'Destination SHA-256 verification failed; local experiment retained: {final}')
    return expected


def append_index(dest, manifest, final, digest):
    path = dest/'archive-index.csv'
    with path.open('a+', newline='', encoding='utf-8') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        header = next(csv.reader(stream), None)
        if header is not None and header != list(INDEX_FIELDS):
            raise ArchiveError(f'Unexpected archive index header: {path}')
        stream.seek(0, os.SEEK_END)
        size = os.fstat(stream.fileno()).st_size
        if size and os.pread(stream.fileno(), 1, size-1) not in (b'\n', b'\r'):
            stream.write('\n')
        writer = csv.DictWriter(stream, fieldnames=INDEX_FIELDS, lineterminator='\n')
        if header is None:
            writer.writeheader()
        writer.writerow(dict(archived_utc=manifest['archived_utc'], fork=manifest['fork'],
                             exp_dir=manifest['exp_dir'], zip_relative_path=final.relative_to(dest).as_posix(),
                             files=manifest['included_files'], bytes=manifest['included_bytes'], sha256=digest,
                             branch=manifest['branch'], head_sha=manifest['head_sha']))
        stream.flush()
        os.fsync(stream.fileno())


def remove_retry(name, *, parent_fd, directory=False):
    remove = os.rmdir if directory else os.unlink
    try:
        remove(name, dir_fd=parent_fd)
    except PermissionError:
        # POSIX deletion needs permission on the containing directory. fchmod
        # cannot follow a substituted pathname, unlike Path.chmod on a retry.
        os.fchmod(parent_fd, os.fstat(parent_fd).st_mode | stat.S_IWUSR | stat.S_IXUSR)
        remove(name, dir_fd=parent_fd)


def prune_files(exp, tracked, *, locks, root_fd):
    """Never follow directory symlinks, including those in nested git checkouts."""
    # Git may precompose names while macOS scandir returns decomposed names.
    # Confirm file identity too, so case-sensitive volumes keep distinct files.
    def alias(path, info):
        return (unicodedata.normalize('NFD', str(path)).casefold(), info.st_dev, info.st_ino)
    preserved = tracked | {p for p in locks if p.is_relative_to(exp)}
    aliases = set()
    if sys.platform == 'darwin':
        for path in preserved:
            try:
                aliases.add(alias(path, os.stat(path.relative_to(exp), dir_fd=root_fd, follow_symlinks=False)))
            except FileNotFoundError:
                pass
    freed = 0
    def visit(directory, fd):
        nonlocal freed
        with os.scandir(fd) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        for entry in children:
            path = directory/entry.name  # Logical names are used only to preserve tracked paths.
            info = entry.stat(follow_symlinks=False)
            if path in preserved or (aliases and alias(path, info) in aliases):
                continue  # Also preserve gitlinks (tracked submodule directories).
            if stat.S_ISDIR(info.st_mode):
                with directory_fd(entry.name, parent_fd=fd) as child_fd:
                    if not os.path.samestat(info, os.fstat(child_fd)):
                        raise ArchiveError(f'Directory changed while pruning: {path}')
                    visit(path, child_fd)
                    with os.scandir(child_fd) as remaining:
                        empty = next(remaining, None) is None
                    if not os.path.samestat(info, os.stat(entry.name, dir_fd=fd, follow_symlinks=False)):
                        raise ArchiveError(f'Directory changed while pruning: {path}')
                    if empty:
                        remove_retry(entry.name, parent_fd=fd, directory=True)
            else:
                remove_retry(entry.name, parent_fd=fd)
                freed += info.st_size
    visit(exp, root_fd)
    return freed


def write_marker(root_fd, record):
    """Keep marker creation anchored to the same experiment directory as pruning."""
    temporary = '.ARCHIVED.json.'+uuid4().hex+'.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(record, stream, indent=2, ensure_ascii=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, 'ARCHIVED.json', src_dir_fd=root_fd, dst_dir_fd=root_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass


def archive(exp_dir, *, destination=None, include_work=False, prune=False, yes=False, dry_run=False, new_zip=False):
    if destination is None:
        raise ArchiveError('No archive destination configured; use --dest DIR or archive.destination in user tool settings')
    exp = Path(exp_dir).expanduser().resolve()
    dest = Path(destination).expanduser().resolve()
    if not exp.is_dir():
        raise ArchiveError(f'Experiment directory does not exist: {exp}')
    original_root = exp.stat()
    if not dest.exists() or not dest.is_dir():
        raise ArchiveError(f'Archive destination root must already exist: {dest}')
    repo = Path(git(exp, 'rev-parse', '--show-toplevel')).resolve()
    if exp == repo or not exp.is_relative_to(repo):
        raise ArchiveError('EXP_DIR must be a directory inside the fork, not the fork root')
    if dest.is_relative_to(exp) or (dest/repo.name).resolve().is_relative_to(exp):
        raise ArchiveError('Archive destination must be outside EXP_DIR')
    tracked = tracked_paths(repo, exp) if prune else set()
    if prune and exp/'ARCHIVED.json' in tracked:
        raise ArchiveError('Cannot prune: ARCHIVED.json is tracked by git')
    try:
        with idle_experiment(exp, dry_run=dry_run) as locks:
            if dry_run:
                totals = {}
                for _ in walk(exp, include_work, totals, count_excluded=True):
                    pass
                print_plan(exp, totals, prune=prune, tracked=tracked, locks=locks)
                return None
            manifest = manifest_for(exp, repo, include_work)
            reused = None
            if prune and not new_zip:
                reused = reusable_archive(dest, repo, exp, include_work, manifest['exclusion_rules'])
            with ExitStack() as scope:
                if reused:
                    final, digest, snapshots = reused
                    print(f'Reusing existing archive with identical contents: {final}\nSHA-256: {digest}')
                else:
                    # A sibling of the fork avoids both EXP_DIR and streamed Drive storage.
                    temp = scope.enter_context(tempfile.NamedTemporaryFile(
                        prefix=f'.{repo.name}-archive-', suffix='.zip', dir=repo.parent))
                    local = Path(temp.name)
                    snapshots = write_zip(local, exp, repo, manifest, include_work)
                    with destination_slot(dest, repo.name, exp.name, manifest['archived_utc'][:10]) as slot:
                        final, partial, sidecar, stream = slot
                        digest = copy_verified(local, partial, final, stream)
                        with sidecar.open('x', encoding='utf-8') as output:
                            json.dump(dict(manifest, sha256=digest), output, indent=2, ensure_ascii=True)
                            output.write('\n')
                            output.flush()
                            os.fsync(output.fileno())
                        append_index(dest, manifest, final, digest)
                    print(f'Archived to {final}\nFiles: {manifest["included_files"]}; '
                          f'bytes before compression: {manifest["included_bytes"]}; zip bytes: {local.stat().st_size}\n'
                          f'SHA-256: {digest}')
                if prune:
                    if not yes:
                        try:
                            answer = input('Type the experiment folder name to delete local files: ')
                        except EOFError:
                            answer = ''
                        if answer != exp.name:
                            print('Prune cancelled; local files retained.')
                            return final
                    current = {p: fingerprint(info) for p, info in walk(exp, include_work, {})}
                    if current != snapshots or tracked_paths(repo, exp) != tracked:
                        raise ArchiveError('Experiment files or tracked paths changed; refusing to prune')
                    with directory_fd(exp) as root_fd:
                        if not os.path.samestat(original_root, os.fstat(root_fd)):
                            raise ArchiveError('Experiment directory changed; refusing to prune')
                        freed = prune_files(exp, tracked, locks=locks, root_fd=root_fd)
                        write_marker(root_fd, dict(destination=str(final), sha256=digest,
                                                  archived_utc=manifest['archived_utc'],
                                                  retained_status_files=status_files(root_fd)))
                    print(f'Bytes freed: {freed} (logical file bytes removed)')
                return final
    except (OSError, zipfile.BadZipFile, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ArchiveError(f'Archive failed: {exc}; local pruning requires a fully verified copy') from exc
