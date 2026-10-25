"""Synthetic evidence and terminals only: never inspect a real experiment."""
from datetime import datetime, timedelta, timezone
import io
import os

import pytest

from test_survival.config import STAGES, stage_hash, validate
from test_survival.io import read_json, write_csv, write_json
from test_survival.panel import LiveProgress, Panel, panel_lines, watch
from test_survival.pipeline import run
from test_survival.progress import generation_progress, snapshot


@pytest.fixture
def config():
    return validate({'name': 'panel', 'project': {'repo': '/unused'}, 'anchor': 't',
                     'generators': [{'name': 'k', 'method': 'ktester', 'tool_path': '/unused'}]})


class Terminal(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        return 2


def generating(exp):
    start = datetime.now(timezone.utc)-timedelta(seconds=100)
    write_json(exp/'state/generate.json', {'status': 'running', 'started': start.isoformat()})
    write_json(exp/'population/scope.json', {'m': 3, 'classes': [{'class': 'A', 'm': 3}]})
    return start


def evidence(exp, base='work/ktester/k/rounds'):
    root = exp/base/'round1'
    (root/'project/test_classes').mkdir(parents=True)
    (root/'project/test_classes/Framework.java').write_text('class Framework {}')
    response = root/'project/responses/A/gencode_response.md'
    response.parent.mkdir(parents=True)
    response.write_text('finished generation')
    write_json(root/'raw-responses/0001/call.json', {'status': 'ok'})
    write_json(root/'raw-responses/0001/response.json', {})
    write_json(root/'raw-responses/0002/call.json', {'status': 'error'})
    write_json(root/'progress/B.json', {'class': 'B_Test', 'status': 'completed'})
    write_json(root/'progress/C.json', {'class': 'C_Test', 'status': 'running'})
    return root


def test_author_methods_ignore_framework_and_deduplicate_archive(tmp_path, config):
    generating(tmp_path)
    evidence(tmp_path)
    evidence(tmp_path, 'generated/k')
    p = snapshot(tmp_path, 'generate', config)
    assert (p['done'], p['total'], p['unit'], p['calls']) == (2, 3, 'methods', 2)
    assert p['responses'] == 1
    assert p['repair'] == 'C_Test'
    assert p['remaining'] > 0
    assert p['eta'] != 'unknown'


def test_old_author_worker_outputs_and_partial_response(tmp_path, config):
    generating(tmp_path)
    root = evidence(tmp_path)
    for path in (root/'progress').glob('*'):
        path.unlink()
    (root/'generation.log').write_text('INFO try to repair test class /out/A_Test.java...\n')
    write_json(root/'time/time_records.json', {'details': {'B': {'check_test_class': 3}}})
    p = generation_progress(tmp_path, config, {})
    assert len(p['completed']) == 2
    assert p['repair'] is None  # A log start cannot prove this class is still active.
    (root/'raw-responses/0003').mkdir()
    (root/'raw-responses/0003/response.json').write_text('{')
    # Response files are counted independently of an in-flight call record.
    assert generation_progress(tmp_path, config, {})['calls'] == 2


def test_native_methods_count_completed_jobs_not_retries(tmp_path, config):
    config['generators'] = [{'name': 'o', 'method': 'ouedraogo', 'params': {'prompts': ['ZSL', 'FSL']}, 'rounds': {'max': 2}}]
    generating(tmp_path)
    root = tmp_path/'generated/o/round1'
    write_json(root/'records/A-ZSL.json', {'class_name': 'A', 'technique': 'ZSL', 'status': 'ok'})
    for suffix in ('', '/attempt-2'):
        write_json(root/f'runs/A/ZSL{suffix}/run.json', {'status': 'ok'})
    p = snapshot(tmp_path, 'generate', config)
    assert (p['done'], p['total'], p['calls']) == (3, 12, 2)
    write_json(tmp_path/'generated/o/rounds.json', {'stopped': 'zero-compile'})
    assert snapshot(tmp_path, 'generate', config)['total'] == 6


def test_two_lines_have_stage_marks_and_metrics(tmp_path, config):
    generating(tmp_path)
    evidence(tmp_path)
    write_json(tmp_path/'state/prepare.json', {'status': 'completed', 'config_hash': stage_hash(config, 'prepare')})
    first, second = panel_lines(tmp_path, config, 250)
    for text in ('generate [', '67%', '2/3 methods', 'calls 2', 'remaining ', 'elapsed ', 'estimated finish '):
        assert text in first
    assert '+prepare' in second
    assert '>generate' in second
    assert '-report' in second
    assert all(stage in second for stage in STAGES)
    compact, _ = panel_lines(tmp_path, config, 80)
    assert len(compact) < 80
    assert 'ETA ' in compact


def test_terminal_scroll_region_resize_cleanup_and_no_escapes_when_disabled(tmp_path, config, monkeypatch):
    terminal = Terminal()
    size = os.terminal_size((160, 24))
    monkeypatch.setattr(os, 'get_terminal_size', lambda fd: size)
    panel = Panel(tmp_path, config, stream=terminal)
    panel.refresh()
    assert '\x1b[1;22r' in terminal.getvalue()
    assert '\x1b[23;1H' in terminal.getvalue()
    assert '\x1b[24;1H' in terminal.getvalue()
    size = os.terminal_size((100, 30))
    panel.refresh()
    assert '\x1b[1;28r' in terminal.getvalue()
    panel.close()
    assert terminal.getvalue().endswith('\x1b[29;1H')
    assert '\x1b[r' in terminal.getvalue()
    for stream, disabled in ((io.StringIO(), False), (Terminal(), True)):
        panel = Panel(tmp_path, config, stream=stream, disabled=disabled)
        panel.refresh()
        panel.close()
        assert stream.getvalue() == ''


def test_foreground_events_cleanup_on_failure_and_plain_logs(tmp_path, config, monkeypatch, capsys):
    import sys
    terminal = Terminal()
    monkeypatch.setattr(sys, 'stderr', terminal)
    def fail(exp, config):
        raise RuntimeError('synthetic failure')
    with pytest.raises(RuntimeError, match='synthetic failure'):
        run(tmp_path, config, until='prepare', handlers={'prepare': fail})
    assert 'prepare: started' in capsys.readouterr().out
    assert 'prepare: failed (RuntimeError)' in (tmp_path/'state/log.txt').read_text()
    assert '\x1b' not in (tmp_path/'state/log.txt').read_text()
    assert 'prepare [' in terminal.getvalue()
    assert '\x1b[r' in terminal.getvalue()
    assert read_json(tmp_path/'state/run.json')['status'] == 'failed'


def test_no_panel_still_writes_two_minute_author_heartbeat(tmp_path, config, monkeypatch):
    import test_survival.panel as module
    generating(tmp_path)
    evidence(tmp_path)
    clock = [0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monitor = LiveProgress(tmp_path, config, disabled=True)
    clock[0] = 119
    monitor.refresh()
    assert not (tmp_path/'state/log.txt').exists()
    clock[0] = 120
    monitor.refresh()
    text = (tmp_path/'state/log.txt').read_text()
    assert '2/3 methods; 1 responses so far; class under repair: C_Test' in text
    monitor.refresh()
    assert (tmp_path/'state/log.txt').read_text() == text
    clock[0] = 240
    monitor.refresh()
    assert len((tmp_path/'state/log.txt').read_text().splitlines()) == 2


def test_watch_polls_five_seconds_finishes_and_never_writes(tmp_path, config, monkeypatch):
    import test_survival.panel as module
    import test_survival.pipeline as pipeline
    terminal = Terminal()
    monkeypatch.setattr(module.sys, 'stderr', terminal)
    write_json(tmp_path/'state/run.json', {'status': 'running', 'pid': 42})
    original = (tmp_path/'state/run.json').read_bytes()
    alive = iter((True, False))
    monkeypatch.setattr(pipeline, 'process_alive', lambda pid: next(alive, False))
    sleeps = []
    monkeypatch.setattr(module.time, 'sleep', sleeps.append)
    watch(tmp_path, config)
    assert sleeps == [5]
    assert terminal.getvalue().count('prepare [') == 2
    assert '\x1b[r' in terminal.getvalue()
    assert (tmp_path/'state/run.json').read_bytes() == original
    assert list((tmp_path/'state').iterdir()) == [tmp_path/'state/run.json']


def test_watch_ctrl_c_restores_terminal(tmp_path, config, monkeypatch):
    import test_survival.panel as module
    import test_survival.pipeline as pipeline
    terminal = Terminal()
    monkeypatch.setattr(module.sys, 'stderr', terminal)
    write_json(tmp_path/'state/run.json', {'status': 'running', 'pid': 42})
    monkeypatch.setattr(pipeline, 'process_alive', lambda pid: True)
    def interrupt(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(module.time, 'sleep', interrupt)
    watch(tmp_path, config)
    assert '\x1b[r' in terminal.getvalue()
    assert read_json(tmp_path/'state/run.json')['status'] == 'running'


def test_control_counts_methods_and_report_counts_fresh_tables(tmp_path, config):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    adapter = JavaMavenAdapter(tmp_path, config)
    (tmp_path/'T.java').write_text('class T {}')
    methods = [{'id': 'one', 'file': 'T.java'}, {'id': 'two', 'file': 'T.java'}]
    write_json(tmp_path/'results/frozen-denominators.json', {'dev': methods})
    write_json(tmp_path/'results/anchor.json', {'sha': 'anchor'})
    write_json(tmp_path/'results/control/0-dev.json', {'sha': 'anchor', 'input_sha256': adapter.run_fingerprint(methods),
               'runner_sha256': adapter.provenance(), 'methods': [{'state': 'pass'}, {'state': 'fail'}]})
    p = snapshot(tmp_path, 'control', config)
    assert (p['done'], p['total'], p['unit']) == (2, 2, 'methods')
    start = datetime.now(timezone.utc)-timedelta(seconds=10)
    write_json(tmp_path/'state/report.json', {'status': 'running', 'started': start.isoformat()})
    (tmp_path/'results/table1-survival-by-event.md').write_text('table')
    p = snapshot(tmp_path, 'report', config)
    assert (p['done'], p['total'], p['unit']) == (1, 5, 'tables')
    os.utime(tmp_path/'results/table1-survival-by-event.md', (0, 0))
    assert snapshot(tmp_path, 'report', config)['done'] == 0


def test_cli_flags_and_non_tty_watch(tmp_path, config, capsys):
    from test_survival.cli import parser
    assert parser().parse_args(['run', str(tmp_path), '--no-panel']).no_panel
    args = parser().parse_args(['status', str(tmp_path), '--watch', '--no-panel'])
    assert args.watch and args.no_panel
    watch(tmp_path, config, no_panel=True)
    captured = capsys.readouterr()
    assert 'prepare    pending' in captured.out
    assert '\x1b' not in captured.out + captured.err


def test_foreground_refreshes_while_no_progress_events_arrive(tmp_path, config):
    monitor = LiveProgress(tmp_path, config, disabled=True)
    waits, refreshes = [], []
    class Wake:
        def wait(self, interval):
            waits.append(interval)
            if len(waits) == 2:
                monitor.stop.set()
            return False
        def clear(self):
            pass
    monitor.wake = Wake()
    monitor.refresh = lambda: refreshes.append(True)
    monitor.loop()
    assert waits == [5, 5]
    assert len(refreshes) == 3  # Initial, idle, and final snapshots.


def test_foreground_no_panel_and_ctrl_c_cleanup(tmp_path, config, monkeypatch):
    import sys
    terminal = Terminal()
    monkeypatch.setattr(sys, 'stderr', terminal)
    def interrupt(exp, config):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, config, until='prepare', no_panel=True, handlers={'prepare': interrupt})
    assert terminal.getvalue() == ''
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, config, until='prepare', handlers={'prepare': interrupt})
    assert '\x1b[r' in terminal.getvalue()
    assert read_json(tmp_path/'state/run.json')['status'] == 'failed'


@pytest.mark.parametrize('outcome', ['completed', 'idle', 'interrupt'])
def test_watch_keeps_final_plain_status_after_terminal_reset(tmp_path, config, monkeypatch, outcome):
    import test_survival.panel as module
    import test_survival.pipeline as pipeline
    output = Terminal()
    monkeypatch.setattr(module.sys, 'stderr', output)
    monkeypatch.setattr(module.sys, 'stdout', output)
    if outcome != 'idle':
        write_json(tmp_path/'state/run.json', {'status': 'running' if outcome == 'interrupt' else 'completed', 'pid': 42})
    monkeypatch.setattr(pipeline, 'process_alive', lambda pid: True)
    def interrupt(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(module.time, 'sleep', interrupt)
    watch(tmp_path, config)
    text = output.getvalue()
    assert text.index('\x1b[r') < text.index('prepare    pending')
    assert text.endswith('\n')


def test_progress_events_do_not_block_workers_on_rendering(tmp_path, config):
    import threading
    from test_survival.panel import notify
    entered, release, returned, next_render = (threading.Event() for _ in range(4))
    monitor = LiveProgress(tmp_path, config, disabled=True)
    renders = []
    def render():
        renders.append(threading.current_thread().name)
        entered.set()
        assert release.wait(2)
        if len(renders) > 1:
            next_render.set()
    monitor.panel.refresh = render
    try:
        with monitor:
            assert entered.wait(2)
            worker = threading.Thread(target=lambda: (notify(tmp_path), returned.set()))
            worker.start()
            assert returned.wait(1), 'Progress notification blocked on an evidence scan'
            worker.join(1)
            release.set()
            assert next_render.wait(2), 'An event during rendering was lost'
    finally:
        release.set()
    assert all(name == 'survival-progress' for name in renders)


@pytest.mark.parametrize('error', [AttributeError, IndexError])
def test_renderer_failure_cannot_fail_stage(tmp_path, config, monkeypatch, error):
    import test_survival.panel as module
    def fail(self):
        raise error('broken progress evidence')
    monkeypatch.setattr(module.Panel, 'refresh', fail)
    assert run(tmp_path, config, until='prepare', handlers={'prepare': lambda *args: 'ok'}) == ['prepare']
    assert read_json(tmp_path/'state/run.json')['status'] == 'completed'


def test_renderer_failure_does_not_suppress_author_heartbeat(tmp_path, config, monkeypatch):
    generating(tmp_path)
    evidence(tmp_path)
    monitor = LiveProgress(tmp_path, config, disabled=True)
    monitor.last_log -= 121
    def broken():
        raise AttributeError('invalid render record')
    monkeypatch.setattr(monitor.panel, 'refresh', broken)
    monitor.refresh()
    assert '2/3 methods' in (tmp_path/'state/log.txt').read_text()


def test_bad_author_progress_recovers_on_next_refresh(tmp_path, config):
    generating(tmp_path)
    root = evidence(tmp_path)
    monitor = LiveProgress(tmp_path, config, disabled=True)
    monitor.last_log -= 121
    write_json(root/'progress/C.json', [])
    monitor.refresh()  # Unexpected JSON shape must not escape or kill the loop.
    assert not (tmp_path/'state/log.txt').exists()
    write_json(root/'progress/C.json', {'class': 'C_Test', 'status': 'running'})
    monitor.refresh()
    assert 'class under repair: C_Test' in (tmp_path/'state/log.txt').read_text()


@pytest.mark.parametrize('options', [{'stage': 'report'}, {'group': 'k'},
                                     {'stage': 'generate', 'group': 'missing'},
                                     {'stage': 'scope', 'until': 'report'}])
def test_invalid_run_does_not_touch_terminal_or_signals(tmp_path, config, monkeypatch, options):
    import signal
    import sys
    from test_survival.pipeline import PipelineError
    terminal = Terminal()
    monkeypatch.setattr(sys, 'stderr', terminal)
    def unexpected(*args):
        pytest.fail('Rejected run changed a signal handler')
    monkeypatch.setattr(signal, 'signal', unexpected)
    with pytest.raises(PipelineError):
        run(tmp_path, config, **options)
    assert terminal.getvalue() == ''


@pytest.mark.parametrize('width', [80, 160])
def test_repair_class_remains_visible_on_normal_terminals(tmp_path, config, width):
    generating(tmp_path)
    root = evidence(tmp_path)
    write_json(root/'progress/C.json', {'class': 'com.example.long.package.C_Test', 'status': 'running'})
    lines = panel_lines(tmp_path, config, width)
    assert all(len(line) < width for line in lines)
    assert ' repair C' in '\n'.join(lines)
    assert all(stage in lines[1] for stage in STAGES)


def test_stale_completed_stage_is_distinct_from_pending(tmp_path, config):
    write_json(tmp_path/'state/prepare.json', {'status': 'completed', 'config_hash': 'old'})
    _, stages = panel_lines(tmp_path, config)
    assert '~prepare' in stages and '-scope' in stages


def test_control_reuses_provenance_and_anchor_for_all_groups(tmp_path, config, monkeypatch):
    import test_survival.progress as module
    from test_survival.adapters.java_maven import JavaMavenAdapter
    write_json(tmp_path/'results/frozen-denominators.json', {'dev': [], 'k': []})
    write_json(tmp_path/'results/anchor.json', {'sha': 'anchor'})
    adapter = JavaMavenAdapter(tmp_path, config)
    for group in ('dev', 'k'):
        write_json(tmp_path/f'results/control/0-{group}.json', {'sha': 'anchor', 'methods': [],
                   'input_sha256': adapter.run_fingerprint([]), 'runner_sha256': adapter.provenance()})
    reads, hashes = [], []
    original_read, original_provenance = module.read_json, JavaMavenAdapter.provenance
    def read(path, *args):
        if path.name == 'anchor.json':
            reads.append(path)
        return original_read(path, *args)
    def provenance(self):
        hashes.append(True)
        return original_provenance(self)
    monkeypatch.setattr(module, 'read_json', read)
    monkeypatch.setattr(JavaMavenAdapter, 'provenance', provenance)
    assert snapshot(tmp_path, 'control', config)['total'] == 0
    assert len(reads) == len(hashes) == 1


def test_terminal_write_and_cleanup_errors_do_not_change_run_result(tmp_path, config, monkeypatch):
    import sys
    class BrokenTerminal(Terminal):
        def write(self, text):
            raise RuntimeError('terminal transport disconnected')
    monkeypatch.setattr(sys, 'stderr', BrokenTerminal())
    assert run(tmp_path, config, until='prepare', handlers={'prepare': lambda *args: 'ok'}) == ['prepare']
    assert read_json(tmp_path/'state/run.json')['status'] == 'completed'
