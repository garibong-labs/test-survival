"""Runner lifecycle shared by stages that submit measurement jobs."""
from contextlib import contextmanager


@contextmanager
def measurement_session(adapter, stage, needed):
    if needed:
        adapter.setup_runner()
        with adapter.runner.session(stage):
            yield
    else:
        yield
