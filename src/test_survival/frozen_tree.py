"""Pin a frozen src/test tree beside it, preserving the historical resource hash."""
import hashlib
from pathlib import Path
import shutil
import tempfile

from .adapters.test_data import frozen_test_dirs
from .config import canonical_hash
from .io import read_json, write_json


class FrozenTreeMismatch(RuntimeError):
    """Frozen test data differs from its recorded identity."""


def copy_frozen_tree(source, root):
    """Publish a complete tree. A failed copy never becomes a resumable freeze."""
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root.parent) as temporary:
        staged = Path(temporary)/'src-test-t'
        shutil.copytree(source, staged)
        staged.rename(root)


def verify_existing_tree(root, *, record=True):
    if root.is_dir() or (root.parent/'frozen-test-tree.json').exists():
        frozen_resource_hash(root, record=record)


def frozen_resource_hash(root, *, record=True):
    root = Path(root)
    marker = root.parent/'frozen-test-tree.json'
    saved = read_json(marker)
    files = {}
    resources = {name: [] for name in frozen_test_dirs(root)}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            relative = path.relative_to(root)
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            files[relative.as_posix()] = digest
            if len(relative.parts) > 1 and relative.parts[0] in resources:
                resources[relative.parts[0]].append((Path(*relative.parts[1:]).as_posix(), digest))
    digest = canonical_hash({name: canonical_hash(rows) for name, rows in resources.items()})
    if saved is not None:
        old = saved['files']
        changed = sorted(k for k in old.keys() & files.keys() if old[k] != files[k])
        added = sorted(files.keys() - old.keys())
        removed = sorted(old.keys() - files.keys())
        if changed or added or removed or saved['resource_tree_hash'] != digest or saved['present'] != root.is_dir():
            detail = [f'{kind} {path}' for kind, paths in [('changed', changed), ('added', added),
                      ('removed', removed)] for path in paths]
            if not detail:
                detail.append('resource directory layout changed')
            raise FrozenTreeMismatch(f'Frozen test tree changed at {root}: '+', '.join(detail))
        return saved['resource_tree_hash']
    # Older experiments may already carry an aggregate in the anchor batch or
    # pool. Never adopt a conflicting tree as its first per-file manifest.
    exp = root.parent.parent
    anchor = read_json(exp/'results/baseline-batches/0.json', {})
    members = [*anchor.get('methods', []), *read_json(exp/'pool.json', [])]
    if any(t.get('resource_tree_hash', digest) != digest for t in members):
        raise FrozenTreeMismatch(
            f'Frozen test tree at {root} differs from its recorded resource_tree_hash (no historical per-file manifest)')
    if record:
        write_json(marker, dict(resource_tree_hash=digest, files=files, present=root.is_dir()))
    return digest
