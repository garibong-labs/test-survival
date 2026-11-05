"""Atomic result files shared by stages."""
import csv
import gzip
import hashlib
import shutil
import stat
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


def now():
    return datetime.now(timezone.utc).isoformat()


def compressed_result(path):
    """Only bulk measurement files, including the same paths in nested pools."""
    parts = Path(path).parts
    for i, part in enumerate(parts[:-2]):
        if part != 'results':
            continue
        folder = parts[i + 1]
        name = parts[-1].removesuffix('.gz')
        if folder in ('raw', 'raw-coverage', 'raw-derived') and name.endswith('.json'):
            return True
        if folder == 'coverage' and (name.endswith('.xml') or name.endswith('-lines.csv')):
            return True
        if folder == 'dev-method-coverage-t':
            return True
    return False


def resolve_path(path):
    """Resolve a logical result name; reject conflicting old/new representations."""
    path = Path(path)
    if path.suffix == '.gz':
        return path
    zipped = path.with_name(path.name + '.gz')
    if path.is_file():
        if zipped.is_file():
            with path.open('rb') as old, gzip.open(zipped, 'rb') as new:
                while True:
                    left, right = old.read(1024 * 1024), new.read(1024 * 1024)
                    if left != right:
                        raise ValueError(f'Conflicting plain and gzip results: {path}')
                    if not left:
                        break
        return path
    return zipped if zipped.is_file() else path


def result_exists(path):
    return resolve_path(path).is_file()


def open_result(path, mode='rt', **kwargs):
    """Open a plain or gzip result for reading, without changing its logical name."""
    if mode not in ('r', 'rt', 'rb'):
        raise ValueError('open_result is read-only')
    path = resolve_path(path)
    return gzip.open(path, mode, **kwargs) if path.suffix == '.gz' else path.open(mode, **kwargs)


def read_bytes(path):
    with open_result(path, 'rb') as stream:
        return stream.read()


def result_hash(path):
    digest = hashlib.sha256()
    with open_result(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def logical_glob(folder, pattern='*'):
    """Yield each logical result once, regardless of its storage encoding."""
    paths = {p.with_suffix('') if p.suffix == '.gz' else p
             for p in Path(folder).glob('*') if p.is_file()}
    for path in sorted(paths):
        if path.match(pattern):
            resolve_path(path)  # Reject ambiguity even for count-only readers.
            yield path


def compress_file(path):
    """Publish a complete gzip stream before removing the plain successful result."""
    path = Path(path)
    target = path.with_name(path.name + '.gz')
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=target.name+'.', suffix='.tmp')
    try:
        os.fchmod(fd, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(fd, 'wb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=6, mtime=0, filename='') as zipped:
                with path.open('rb') as source:
                    shutil.copyfileobj(source, zipped)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, target)
        path.unlink()
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def atomic_text(path, text, *, compress=False, permissions=None):
    path = Path(path)
    compress = compress or path.suffix == '.gz'
    target = path.with_name(path.name + '.gz') if compress and path.suffix != '.gz' else path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=target.name+'.', suffix='.tmp')
    try:
        if permissions is not None:os.fchmod(fd, permissions)
        data = text.encode('utf-8')
        with os.fdopen(fd, 'wb') as stream:
            stream.write(gzip.compress(data, compresslevel=6, mtime=0) if compress else data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        # A crash before unlink leaves equal copies, which readers accept. A
        # conflicting interrupted replacement fails closed instead of reading stale data.
        if compress and target != path:
            path.unlink(missing_ok=True)
        elif not compress and compressed_result(path):
            path.with_name(path.name+'.gz').unlink(missing_ok=True)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value, *, compress=False):
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n', compress=compress)


def read_json(path, default=None):
    try:
        with open_result(path, encoding='utf-8') as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default


def read_csv(path):
    with open_result(path, newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def write_csv(path, rows, fields, *, compress=False, permissions=None):
    import io
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(path, stream.getvalue(), compress=compress, permissions=permissions)
