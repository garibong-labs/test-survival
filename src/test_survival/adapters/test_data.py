"""Test data folders that a frozen developer src/test tree carries into test runs.

Standard library only: the container worker imports it outside the package.
"""
from pathlib import Path


def frozen_test_dirs(root):
    """Folders of a frozen src/test tree that tests may read at run time.

    Maven projects keep test data beside the Java sources under many names
    (resources, suite, input, witness, data, ...). Tests usually open them with
    module-relative paths such as src/test/input/a.xml, so every folder except the
    Java sources goes into the runtime working directory. resources and suite are
    always listed so trees without other folders keep their earlier hashes.
    """
    root = Path(root)
    names = {'resources', 'suite'}
    if root.is_dir():
        names |= {p.name for p in root.iterdir() if p.is_dir() and p.name != 'java'}
    return sorted(names)


def extra_test_dirs(root):
    """Data folders of a frozen src/test tree beyond resources and suite."""
    root = Path(root)
    return [name for name in frozen_test_dirs(root) if name not in ('resources', 'suite') and (root/name).is_dir()]


def staging_identity(root):
    """Names, sizes and modification times of every file staged from a frozen tree.

    The frozen tree is written once at t, so this cheap identity changes only when
    the staging rule or the tree does. It is computed on the host only.
    """
    root = Path(root)
    rows = []
    for name in frozen_test_dirs(root):
        folder = root/name
        if folder.is_dir():
            for path in sorted(folder.rglob('*')):
                if path.is_file():
                    stat = path.stat()
                    rows.append([path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns])
    return rows


def stage_test_data(root, runtime):
    """Replace runtime/src/test with copies of the run-time test data of a frozen tree."""
    import shutil
    root, target = Path(root), Path(runtime)/'src/test'
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in frozen_test_dirs(root):
        if (root/name).is_dir():
            shutil.copytree(root/name, target/name)
