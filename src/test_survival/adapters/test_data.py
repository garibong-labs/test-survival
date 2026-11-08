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
