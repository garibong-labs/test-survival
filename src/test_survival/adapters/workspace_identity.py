"""Container-side Maven dependency identity for workspace restoration (stdlib only)."""
import hashlib
import json
from pathlib import Path
import sys


def identity(classpath):
    entries = [Path(p) for p in Path(classpath).read_text().strip().split(':') if p]
    result = []
    for path in entries:
        if path.is_symlink() or not path.is_file():
            raise ValueError('Unverifiable build dependency: '+str(path))
        with path.open('rb') as stream:
            result.append(dict(path=str(path), sha256=hashlib.file_digest(stream, 'sha256').hexdigest()))
    return result


if __name__ == '__main__':
    # The runner merges stdout/stderr with container startup diagnostics.
    # Publish structured data separately, only after every dependency is hashed.
    Path(sys.argv[2]).write_text(json.dumps(identity(sys.argv[1]), sort_keys=True)+'\n', encoding='utf-8')
