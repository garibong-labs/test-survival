"""Run-local progress suffixes shared by stage workers and nested policy pools."""
from contextlib import contextmanager
from pathlib import Path
from threading import Condition, Lock
from time import monotonic

from .progress import Description


MIN_REFRESH_SECONDS = 30
REFRESH_COST_FACTOR = 10


class LogProgress:
    def __init__(self, *, disabled=False, clock=monotonic):
        self.disabled = disabled
        self.clock = clock
        self.entries = {}
        self.refreshing = set()
        self.changed = Condition()

    def get(self, key, compute, *, refresh=False):
        if self.disabled:
            return None
        with self.changed:
            while key in self.refreshing:
                if not refresh and key in self.entries:
                    return self.entries[key][0]
                # The first line has no suffix to share yet. A final line
                # needs a snapshot taken after its state was published.
                self.changed.wait()
            cached = self.entries.get(key)
            start = self.clock()
            if cached and not refresh and start < cached[1]:
                return cached[0]
            self.refreshing.add(key)
        try:
            try:
                progress = compute()
            except Exception:
                # A partial evidence update must not interrupt the worker.
                progress = cached[0] if cached else Description('elapsed 00:00:00; 0/? items; estimated finish ', 'unknown')
            end = self.clock()
            deadline = end + max(MIN_REFRESH_SECONDS, REFRESH_COST_FACTOR * (end - start))
            with self.changed:
                self.entries[key] = progress, deadline
            return progress
        finally:
            with self.changed:
                self.refreshing.remove(key)
                self.changed.notify_all()


_default = LogProgress()
_runs = {}
_lock = Lock()


@contextmanager
def run_logging(exp, *, no_progress):
    exp = Path(exp).resolve()
    progress = LogProgress(disabled=no_progress)
    with _lock:
        _runs[exp] = progress
    try:
        yield progress
    finally:
        with _lock:
            del _runs[exp]


def for_experiment(exp):
    with _lock:
        if _runs:
            # A pool logs against its own experiment directory in another
            # thread. It inherits the enclosing run's logging settings.
            for root in (exp, *exp.parents):
                if root in _runs:
                    return _runs[root]
    return _default
