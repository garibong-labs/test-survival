"""Bounded dispatch with snapshots taken when a worker slot becomes available."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def dispatch(items, workers, prepare, execute, completed):
    """prepare returns (context, cached result), or None to await earlier work.

    Callbacks run on the coordinator thread; only execute runs on workers.
    Cached results advance the coordinator immediately without occupying a slot.
    """
    items = iter(items)
    item = next(items, None)
    pending = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while item is not None or pending:
            while item is not None and len(pending) < workers:
                for future in [f for f in pending if f.done()]:
                    completed(pending.pop(future), future.result())
                prepared = prepare(item, list(pending.values()))
                if prepared is None:
                    if not pending:
                        raise RuntimeError('Dispatch deferred without earlier work')
                    break
                context, cached = prepared
                if cached is not None:
                    completed(item, cached)
                else:
                    pending[pool.submit(execute, item, context)] = item
                item = next(items, None)
            if pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    completed(pending.pop(future), future.result())
