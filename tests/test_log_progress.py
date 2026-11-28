from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Event
from uuid import UUID

import pytest

from test_survival import cli, log_progress, pipeline
from test_survival.display import UTC_DISPLAY
from test_survival.io import read_json
from test_survival.progress import Description


def test_run_flag_changes_only_log_and_skips_all_1000_computations(tmp_path, monkeypatch):
    calls = []
    stamp = '2026-01-01T00:00:00+00:00'
    def describe(exp, stage, config):
        status = read_json(exp/f'state/{stage}.json')['status']
        calls.append(status)
        return Description(status + ' ', 'unknown')
    def prepare(exp, config):
        for i in range(998):
            pipeline.log(exp, f'prepare: build {i}', config)
        return 'all builds recorded'

    monkeypatch.setattr(pipeline, 'description', describe)
    monkeypatch.setattr(pipeline, 'now', lambda: stamp)
    monkeypatch.setattr(pipeline, 'uuid4', lambda: UUID(int=1))
    monkeypatch.setattr(log_progress, 'LogProgress', partial(log_progress.LogProgress, clock=lambda: 0))
    monkeypatch.setattr(cli, 'run', partial(pipeline.run, handlers={'prepare': prepare}))
    (tmp_path/'experiment.yaml').write_text('name: fixture\nanchor: t\nproject: {repo: /unused}\n')
    args = ['run', str(tmp_path), '--until', 'prepare', '--no-panel', '--force']
    assert cli.main(args) == 0
    assert calls == ['running', 'completed']
    path = tmp_path/'state/log.txt'
    lines = path.read_text().splitlines()
    assert len(lines) == 1000
    assert all(line.endswith(' | running unknown') for line in lines[:-1])
    assert lines[-1].endswith('all builds recorded | completed unknown')
    def evidence():
        return {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file() and p != path}
    saved = evidence()
    calls.clear()
    assert cli.main([*args, '--no-progress']) == 0
    assert not calls
    assert path.read_text().splitlines()[1000:] == [line.partition(' | ')[0] for line in lines]
    assert evidence() == saved


@pytest.mark.parametrize(('cost', 'interval'), [(0.5, 30), (4, 40)])
def test_log_refresh_interval_starts_after_scan_finishes(tmp_path, monkeypatch, cost, interval):
    clock = [0]
    starts = []
    def compute(*args):
        starts.append(clock[0])
        clock[0] += cost
        return Description('test ', 'unknown')
    monkeypatch.setattr(pipeline, 'description', compute)
    with log_progress.run_logging(tmp_path, no_progress=False) as logs:
        logs.clock = lambda: clock[0]
        def log():
            pipeline.log(tmp_path, 'screen: recorded', {}, display=UTC_DISPLAY)
        log()
        clock[0] = cost + interval - .01
        log()
        assert starts == [0]
        clock[0] = cost + interval
        log()
        assert starts == [0, cost + interval]


def test_workers_use_cached_suffix_while_refresh_runs_and_final_refreshes_again(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    calls = []
    def compute(*args):
        calls.append(len(calls) + 1)
        if len(calls) == 2:
            entered.set()
            assert release.wait(5)
        return Description(f'scan {len(calls)} ', 'unknown')
    def log(message, *, final=False):
        pipeline.log(tmp_path, f'screen: {message}', {}, display=UTC_DISPLAY, refresh=final)
    monkeypatch.setattr(pipeline, 'description', compute)
    with log_progress.run_logging(tmp_path, no_progress=False) as logs, ThreadPoolExecutor(3) as workers:
        logs.clock = lambda: 0
        log('start')
        logs.clock = lambda: 31
        refresh = workers.submit(log, 'refresh')
        try:
            assert entered.wait(2)
            workers.submit(log, 'another item').result(timeout=2)
            assert calls == [1, 2]
            assert 'another item | scan 1 unknown' in (tmp_path/'state/log.txt').read_text()
            final = workers.submit(log, 'finished', final=True)
        finally:
            release.set()
        refresh.result(timeout=2)
        final.result(timeout=2)
    assert calls == [1, 2, 3]
    assert 'finished | scan 3 unknown' in (tmp_path/'state/log.txt').read_text()


def test_first_scan_is_shared_and_other_pools_and_stages_can_log(tmp_path, monkeypatch):
    first, second = tmp_path/'one', tmp_path/'two'
    entered, release = Event(), Event()
    calls = []
    def compute(exp, stage, config):
        calls.append((exp, stage))
        if (exp, stage) == (first, 'screen'):
            entered.set()
            assert release.wait(5)
        return Description(f'{exp.name} {stage} ', 'unknown')
    def log(exp, stage):
        pipeline.log(exp, f'{stage}: item', {}, display=UTC_DISPLAY)
    monkeypatch.setattr(pipeline, 'description', compute)
    with log_progress.run_logging(tmp_path, no_progress=False), ThreadPoolExecutor(4) as workers:
        initial = workers.submit(log, first, 'screen')
        try:
            assert entered.wait(2)
            same = workers.submit(log, first, 'screen')
            workers.submit(log, second, 'screen').result(timeout=2)
            workers.submit(log, first, 'generate').result(timeout=2)
        finally:
            release.set()
        initial.result(timeout=2)
        same.result(timeout=2)
    assert calls == [(first, 'screen'), (second, 'screen'), (first, 'generate')]
    assert (first/'state/log.txt').read_text().count('screen: item | one screen unknown') == 2


def test_failed_scan_can_log_and_retry(tmp_path, monkeypatch):
    calls = []
    def compute(*args):
        calls.append(True)
        if len(calls) != 2:
            raise ValueError('partial evidence')
        return Description('recovered ', 'unknown')
    monkeypatch.setattr(pipeline, 'description', compute)
    with log_progress.run_logging(tmp_path, no_progress=False) as logs:
        for second in (0, 1, 30, 60):
            logs.clock = lambda: second
            pipeline.log(tmp_path, 'screen: item', {}, display=UTC_DISPLAY)
    lines = (tmp_path/'state/log.txt').read_text().splitlines()
    assert len(calls) == 3
    assert all('0/? items; estimated finish unknown' in line for line in lines[:2])
    assert all('recovered unknown' in line for line in lines[2:])
