"""Two reserved terminal rows; all experiment inspection is read-only."""
import os
from pathlib import Path
import signal
import sys
import threading
import time

from .config import STAGES, stage_hash
from .io import now, read_json
from .progress import snapshot, duration, generation_progress
from .display import load_display_settings

OUTPUT_LOCK = threading.RLock()
_MONITORS = {}


def panel_lines(exp, config, width=160, *, display=None):
    display = display or load_display_settings()
    states = {stage: read_json(Path(exp)/'state'/f'{stage}.json', {}) for stage in STAGES}
    current = next((s for s in STAGES if states[s].get('status') == 'running'), None)
    if current is None:
        current = max((s for s in STAGES if states[s].get('started')), default=STAGES[0],
                      key=lambda s: states[s].get('started', ''))
    p = snapshot(exp, current, config)
    shown_eta = display.timestamp(p['eta'], timespec='seconds')
    done, total = p['done'], p['total']
    fraction = min(1, done/total) if total else (1 if total == 0 and p['status'] == 'completed' else None)
    bar = '#' * int(10*fraction) + '-' * (10-int(10*fraction)) if fraction is not None else '?'*10
    percent = f'{100*fraction:.0f}%' if fraction is not None else '?%'
    secondary = f'; calls {p["calls"]}' if p.get('calls') is not None else ''
    repair = ','.join(name.rsplit('.', 1)[-1] for name in p.get('repair', '').split(', ')) if p.get('repair') else ''
    first = (f'{current} [{bar}] {percent} {done}/{total if total is not None else "?"} {p["unit"]}'
             f'{secondary}; remaining {duration(p["remaining"])}; elapsed {duration(p["elapsed"])}; '
             f'estimated finish {shown_eta}')
    if len(first) >= width:
        def short(seconds):
            if seconds is None:
                return '?'
            seconds = int(seconds)
            return f'{seconds//3600}h{seconds%3600//60}m' if seconds >= 3600 else f'{seconds//60}m{seconds%60}s'
        bar = '#' * int(5*fraction) + '-' * (5-int(5*fraction)) if fraction is not None else '?'*5
        eta = display.timestamp(p['eta'], timespec='minutes') if p['eta'] != 'unknown' else '?'
        secondary = f' calls:{p["calls"]}' if p.get('calls') is not None else ''
        first = (f'{current} [{bar}] {percent} {done}/{total if total is not None else "?"} {p["unit"]}'
                 f'{secondary} rem {short(p["remaining"])} elapsed {short(p["elapsed"])} ETA {eta}')
        if len(first) >= width:
            # Keep a complete date and offset; never show a clipped clock value.
            small_bar = '#' * int(3*fraction) + '-' * (3-int(3*fraction)) if fraction is not None else '???'
            unit_short = {'methods': 'm', 'builds': 'b', 'calls': 'c', 'files': 'f',
                          'commits': 'c', 'indices': 'i', 'tables': 't', 'items': 'i'}[p['unit']]
            calls = f' c{p["calls"]}' if p.get('calls') is not None else ''
            first = (f'{current} [{small_bar}] {percent} {done}/{total if total is not None else "?"}{unit_short}'
                     f'{calls} r{short(p["remaining"])} e{short(p["elapsed"])} ETA {eta}')
            if len(first) >= width:
                first = f'{current} {percent} ETA {eta}'
            if len(first) >= width:
                first = f'ETA {eta}'
            if len(first) >= width:
                first = 'ETA hidden'
    marks = []
    for stage in STAGES:
        state = states[stage]
        complete = state.get('status') == 'completed' and state.get('config_hash') == stage_hash(config, stage)
        mark = ('>' if stage == current and state.get('status') == 'running' else '+' if complete
                else '~' if state.get('status') == 'completed' else '!' if state.get('status') == 'failed' else '-')
        marks.append(f'{mark}{stage}')
    second = ' '.join(marks)
    if repair:
        # Keep mandatory metrics and all eight stages; use the row with more
        # spare space for the simple repair class name (ellipsized if needed).
        row = 0 if len(first) <= len(second) else 1
        lines = [first, second]
        room = width-1-len(lines[row])-len(' repair ')
        if room >= 3:
            lines[row] += ' repair ' + (repair if len(repair) <= room else repair[:room-2]+'..')
        first, second = lines
    return first, second


class Panel:
    def __init__(self, exp, config, *, disabled=False, stream=None, display=None):
        self.exp, self.config = Path(exp), config
        self.display = display or load_display_settings()
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = not disabled and self.stream.isatty()
        self.size = None

    def refresh(self):
        if not self.enabled:
            return
        try:
            size = os.get_terminal_size(self.stream.fileno())
        except (OSError, ValueError, AttributeError):
            size = os.terminal_size((160, 24))
        # Read outside the terminal critical section; render in one write.
        lines = panel_lines(self.exp, self.config, size.columns, display=self.display)
        with OUTPUT_LOCK:
            cols, rows = size
            if rows < 4:
                self.close()
                return
            prefix = ''
            if size != self.size:
                if self.size:
                    prefix += '\x1b[r'
                    for old_row in (self.size.lines-1, self.size.lines):
                        if old_row <= rows:
                            prefix += f'\x1b[{old_row};1H\x1b[2K'
                prefix += f'\x1b[1;{rows-2}r\x1b[{rows-2};1H'
                self.size = size
            # No newline/wrap in the reserved rows. Control bytes from evidence
            # must never become terminal instructions.
            clean = [''.join(c if ' ' <= c <= '~' else '?' for c in line)[:max(0, cols-1)] for line in lines]
            self.stream.write(prefix + '\x1b7' + f'\x1b[{rows-1};1H\x1b[2K' + clean[0]
                              + f'\x1b[{rows};1H\x1b[2K' + clean[1] + '\x1b8')
            self.stream.flush()

    def close(self):
        with OUTPUT_LOCK:
            if self.size:
                rows = self.size.lines
                try:
                    self.stream.write(f'\x1b[r\x1b[{rows-1};1H\x1b[2K\x1b[{rows};1H\x1b[2K\x1b[{rows-1};1H')
                    self.stream.flush()
                except Exception:
                    pass  # A disconnected terminal must not mask the run result.
                finally:
                    self.size = None


def notify(exp):
    monitor = _MONITORS.get(Path(exp).resolve())
    if monitor:
        monitor.wake.set()


def active_display(exp):
    monitor = _MONITORS.get(Path(exp).resolve())
    return monitor.display if monitor else None


class LiveProgress:
    """Heartbeat also runs without a TTY, for author-tool durable progress."""
    def __init__(self, exp, config, *, disabled=False, display=None):
        self.exp, self.config = Path(exp).resolve(), config
        self.display = display or load_display_settings()
        self.panel = Panel(exp, config, disabled=disabled, display=self.display)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.last_log = time.monotonic()
        self.lock = threading.RLock()

    def refresh(self):
        with self.lock:
            try:
                self.panel.refresh()
            except Exception:
                pass  # A rendering failure must not fail work or its heartbeat.
            try:
                state = read_json(self.exp/'state/generate.json', {})
                if state.get('status') != 'running':
                    self.last_log = time.monotonic()
                elif time.monotonic()-self.last_log >= 120:
                    groups = [g for g in self.config['generators'] if g['method'] == 'ktester'
                              and state.get('group') in (None, g['name'])]
                    if groups:
                        p = generation_progress(self.exp, {**self.config, 'generators': groups}, state)
                        line = (f'{now()} generate: {len(p["completed"])}/{p["total"] if p["total"] is not None else "?"} methods; '
                                f'{p["responses"]} responses so far; class under repair: '
                                f'{"unknown" if p["repair"] is None else p["repair"] or "none"}\n')
                        with OUTPUT_LOCK:
                            with (self.exp/'state/log.txt').open('a') as stream:
                                stream.write(line)
                    self.last_log = time.monotonic()
            except Exception:
                # Partial author output and terminal disconnects must not fail a run.
                pass

    def loop(self):
        while True:
            self.refresh()
            if self.stop.is_set():
                return
            notified = self.wake.wait(5)
            self.wake.clear()
            if notified:
                # Coalesce bursts without making stage/model workers scan files.
                self.stop.wait(.05)

    def __enter__(self):
        _MONITORS[self.exp] = self
        self.thread = threading.Thread(target=self.loop, name='survival-progress', daemon=True)
        self.thread.start()
        self.previous_term = None
        if self.panel.enabled and threading.current_thread() is threading.main_thread():
            self.previous_term = signal.getsignal(signal.SIGTERM)
            def terminate(signum, frame):
                raise SystemExit(128+signum)
            signal.signal(signal.SIGTERM, terminate)
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.wake.set()
        self.thread.join()
        _MONITORS.pop(self.exp, None)
        self.panel.close()
        if self.previous_term is not None:
            signal.signal(signal.SIGTERM, self.previous_term)


def watch(exp, config, *, no_panel=False, display=None):
    from .pipeline import process_alive, status
    display = display or load_display_settings()
    panel = Panel(exp, config, disabled=no_panel, display=display)
    try:
        while True:
            if panel.enabled:
                panel.refresh()
            else:
                print(status(exp, config, display=display), flush=True)
            run = read_json(Path(exp)/'state/run.json', {})
            if run.get('status') != 'running' or not process_alive(run.get('pid')):
                break
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        panel.close()
    if panel.enabled:
        print(status(exp, config, display=display), flush=True)
