"""Policy progress shared by foreground logs and read-only status watchers."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

from ..display import UTC_DISPLAY
from ..io import now, read_json
from ..panel import OUTPUT_LOCK
from ..progress import Description, duration


def description(exp, name, state, *, at=None):
    exp = Path(exp)
    at = at or datetime.now(timezone.utc)
    start = datetime.fromisoformat(state['started']) if state.get('started') else None
    finish = datetime.fromisoformat(state['finished']) if state.get('finished') else None
    elapsed = max(0, ((finish or at)-start).total_seconds()) if start else 0
    fresh = 0
    if name == 'pools':
        plan = read_json(exp/'results/policy/active-plan.json', None)
        if plan is None:
            plan = read_json(exp/'results/policy/plan.json', {})
        total = len(plan['pools']) if 'pools' in plan else None
        done = 0
        for point in plan.get('pools', []):
            group, index = point['group'], point['index']
            child = read_json(exp/f'state/policy-pool-{group}-{index}.json', {})
            output = exp/f'results/policy/pools/{group}/{index}/pool.json'
            if (child.get('status') == 'completed' and child.get('config_hash') == state.get('config_hash')
                    and output.exists()):
                done += 1
                ended = datetime.fromisoformat(child['finished']) if child.get('finished') else None
                fresh += bool(start and ended and ended >= start)
        unit = 'pools'
    elif state.get('outputs'):
        total = len(state['outputs'])
        done = 0
        for relative in state['outputs']:
            output = exp/relative
            if output.exists():
                recent = bool(start and output.stat().st_mtime >= start.timestamp())
                if recent or state.get('status') == 'completed':
                    done += 1
                    fresh += recent
        unit = 'items'
    else:
        total, done, unit = 1, int(state.get('status') == 'completed'), 'items'
    eta = 'unknown'
    if state.get('status') == 'completed' and finish:
        eta = finish.isoformat(timespec='seconds')
    elif state.get('status') == 'running' and fresh and elapsed and total is not None:
        eta = (at+timedelta(seconds=elapsed*max(0, total-done)/fresh)).isoformat(timespec='seconds')
    return Description(f'elapsed {duration(elapsed)}; {done}/{total if total is not None else "?"} {unit}; estimated finish ', eta)


class LiveProgress:
    def __init__(self, exp, *, display=UTC_DISPLAY):
        self.exp = Path(exp)
        self.display = display
        self.active = set()
        self.lock = threading.RLock()
        self.stop = threading.Event()

    def emit(self, name, message):
        state = read_json(self.exp/f'state/policy-{name}.json', {})
        progress = description(self.exp, name, state)
        stamp = now()
        text = f'policy-{name}: {message} | '
        with OUTPUT_LOCK:
            print(f'{self.display.timestamp(stamp)} {text}{progress.render(self.display)}', flush=True)
            with (self.exp/'state/log.txt').open('a') as stream:
                stream.write(f'{stamp} {text}{progress.render()}\n')

    def event(self, name, message, *, running=False):
        with self.lock:
            if running:
                self.active.add(name)
            else:
                self.active.discard(name)
            self.emit(name, message)
            if name.startswith('pool-') and 'pools' in self.active:
                self.emit('pools', 'running')

    def loop(self):
        while not self.stop.wait(5):
            with self.lock:
                for name in sorted(self.active):
                    try:
                        self.emit(name, 'running')
                    except (OSError, ValueError, KeyError, TypeError):
                        pass  # Progress must not fail work during an evidence update.

    def __enter__(self):
        self.thread = threading.Thread(target=self.loop, name='policy-progress', daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()
