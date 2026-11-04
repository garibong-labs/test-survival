"""One-pass, verified experiment archives and conservative local pruning."""
from contextlib import contextmanager, ExitStack
import csv
import ctypes
from datetime import datetime, timezone
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

from .io import read_json, write_json


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


def status_files(exp):
    """Identify restored/resumed state without changing execution provenance."""
    exp = Path(exp)
    paths = [exp/'experiment.yaml', *sorted((exp/'state').glob('*.json'))]
    return {p.relative_to(exp).as_posix(): sha256(p) for p in paths if p.is_file()}


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
        owner = read_json(state/'run-owner.json')
        owner_locks = set()
        if owner is not None:
            if not isinstance(owner, dict) or not isinstance(owner.get('experiment'), str):
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
        stage_states={p.name: read_json(p) for p in sorted((exp/'state').glob('*.json'))},
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
        writer = csv.DictWriter(stream, fieldnames=INDEX_FIELDS, lineterminator='\n')
        if header is None:
            writer.writeheader()
        writer.writerow(dict(archived_utc=manifest['archived_utc'], fork=manifest['fork'],
                             exp_dir=manifest['exp_dir'], zip_relative_path=final.relative_to(dest).as_posix(),
                             files=manifest['included_files'], bytes=manifest['included_bytes'], sha256=digest,
                             branch=manifest['branch'], head_sha=manifest['head_sha']))
        stream.flush()
        os.fsync(stream.fileno())


def remove_retry(path, *, directory=False):
    try:
        path.rmdir() if directory else path.unlink()
    except PermissionError:
        path.parent.chmod(path.parent.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | stat.S_IWUSR | (stat.S_IXUSR if directory else 0))
        path.rmdir() if directory else path.unlink()


def prune_files(exp, tracked, *, locks):
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
                aliases.add(alias(path, path.lstat()))
            except FileNotFoundError:
                pass
    freed = 0
    def visit(directory):
        nonlocal freed
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        for entry in children:
            path = Path(entry.path)
            info = entry.stat(follow_symlinks=False)
            if path in preserved or (aliases and alias(path, info) in aliases):
                continue  # Also preserve gitlinks (tracked submodule directories).
            if stat.S_ISDIR(info.st_mode):
                visit(path)
                if not any(path.iterdir()):
                    remove_retry(path, directory=True)
            else:
                remove_retry(path)
                freed += info.st_size
    visit(exp)
    return freed


def archive(exp_dir, *, destination=None, include_work=False, prune=False, yes=False, dry_run=False):
    if destination is None:
        raise ArchiveError('No archive destination configured; use --dest DIR or archive.destination in user tool settings')
    exp = Path(exp_dir).expanduser().resolve()
    dest = Path(destination).expanduser().resolve()
    if not exp.is_dir():
        raise ArchiveError(f'Experiment directory does not exist: {exp}')
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
            # A sibling of the fork avoids both EXP_DIR and streamed Drive storage.
            with tempfile.NamedTemporaryFile(prefix=f'.{repo.name}-archive-', suffix='.zip', dir=repo.parent) as temp:
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
                    freed = prune_files(exp, tracked, locks=locks)
                    write_json(exp/'ARCHIVED.json', dict(destination=str(final), sha256=digest,
                                                         archived_utc=manifest['archived_utc'],
                                                         retained_status_files=status_files(exp)))
                    print(f'Bytes freed: {freed} (logical file bytes removed)')
                return final
    except (OSError, zipfile.BadZipFile, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ArchiveError(f'Archive failed: {exc}; local pruning requires a fully verified copy') from exc
