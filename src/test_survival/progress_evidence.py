"""Process-local memoization for read-only progress checks."""
from threading import Lock

from .io import resolve_path


def file_stamp(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    # Inode and change time also distinguish atomic replacements that
    # preserve both size and the original modification time.
    return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino, stat.st_ctime_ns


class FileMemo:
    """Keep one revision per logical path, including the check's other inputs."""
    def __init__(self):
        self.entries = {}
        self.lock = Lock()

    def get(self, path, inputs, check):
        # Honor plain/gzip conflict detection even on a cache hit.
        physical = resolve_path(path)
        revision = physical, file_stamp(physical), inputs
        with self.lock:
            cached = self.entries.get(path)
            if cached is not None and cached[0] == revision:
                return cached[1]
        value = check()
        with self.lock:
            self.entries[path] = revision, value
        return value
