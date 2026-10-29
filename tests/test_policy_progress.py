from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from test_survival.display import DisplaySettings
from test_survival.io import write_json
from test_survival.policy.progress import LiveProgress, description


def test_pool_progress_excludes_cached_work_from_rate_and_failed_or_missing_outputs(tmp_path):
    points = [dict(group='dev-scope', index=i) for i in range(5)]
    write_json(tmp_path/'results/policy/plan.json', dict(pools=points))
    state = dict(status='running', started='2026-01-01T00:00:00+00:00', config_hash='current')
    for i, status, digest, finished in [
        (0, 'completed', 'current', '2025-12-31T00:00:00+00:00'),
        (1, 'completed', 'current', '2026-01-01T00:00:10+00:00'),
        (2, 'failed', 'current', '2026-01-01T00:00:10+00:00'),
        (3, 'completed', 'old', '2026-01-01T00:00:10+00:00'),
        (4, 'completed', 'current', '2026-01-01T00:00:10+00:00'),
    ]:
        write_json(tmp_path/f'state/policy-pool-dev-scope-{i}.json', dict(status=status, config_hash=digest, finished=finished))
        if i != 4:
            write_json(tmp_path/f'results/policy/pools/dev-scope/{i}/pool.json', [])
    actual = description(tmp_path, 'pools', state, at=datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc))
    assert actual.prefix == 'elapsed 00:00:10; 2/5 pools; estimated finish '
    assert actual.eta == '2026-01-01T00:00:40+00:00'
    assert description(tmp_path, 'pools', {**state, 'status':'failed'}).eta == 'unknown'


def test_policy_logs_progress_with_local_display_and_utc_evidence(tmp_path, capsys):
    write_json(tmp_path/'state/policy-report.json', dict(status='completed', started='2026-01-01T00:00:00+00:00', finished='2026-01-01T00:01:00+00:00'))
    display = DisplaySettings(ZoneInfo('Asia/Seoul'))
    with LiveProgress(tmp_path, display=display) as progress:
        progress.event('report', 'completed')
    assert not progress.thread.is_alive()
    output = capsys.readouterr().out
    assert 'policy-report: completed | elapsed 00:01:00; 1/1 items; estimated finish 2026-01-01T09:01:00+09:00' in output
    assert 'estimated finish 2026-01-01T00:01:00+00:00' in (tmp_path/'state/log.txt').read_text()


def test_policy_progress_heartbeat(tmp_path, monkeypatch, capsys):
    write_json(tmp_path/'state/policy-pool-dev-scope-21.json', dict(status='running'))
    progress = LiveProgress(tmp_path)
    progress.active.add('pool-dev-scope-21')
    waits = iter([False, True])
    monkeypatch.setattr(progress.stop, 'wait', lambda seconds:next(waits))
    progress.loop()
    assert 'policy-pool-dev-scope-21: running | elapsed 00:00:00; 0/1 items; estimated finish unknown' in capsys.readouterr().out


def test_policy_status_watch_refreshes_until_run_stops(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from test_survival.cli import main
    from test_survival.policy import pipeline
    monkeypatch.setattr(pipeline, 'load', lambda exp:{})
    monkeypatch.setattr(pipeline, 'Baseline', lambda exp, config:SimpleNamespace(exp=Path(exp)))
    monkeypatch.setattr(pipeline, 'process_alive', lambda pid:True)
    write_json(tmp_path/'state/policy-report.json', dict(status='running', pid=123))
    write_json(tmp_path/'state/run.json', dict(status='running', pid=123))
    sleeps = []
    def finish(seconds):
        sleeps.append(seconds)
        write_json(tmp_path/'state/run.json', dict(status='completed'))
        write_json(tmp_path/'state/policy-report.json', dict(status='completed'))
    monkeypatch.setattr(pipeline.time, 'sleep', finish)
    assert main(['policy', 'status', str(tmp_path), '--watch']) == 0
    assert sleeps == [5]
    output = capsys.readouterr().out
    assert 'policy-report.json: running | elapsed 00:00:00; 0/1 items; estimated finish unknown' in output
    assert 'policy-report.json: completed | elapsed 00:00:00; 1/1 items; estimated finish unknown' in output


def test_policy_watch_stops_for_dead_owner_and_reports_interruption(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from test_survival.policy import pipeline
    monkeypatch.setattr(pipeline, 'Baseline', lambda exp, config:SimpleNamespace(exp=Path(exp)))
    monkeypatch.setattr(pipeline, 'process_alive', lambda pid:False)
    def unexpected_sleep(seconds):
        raise AssertionError('Must not wait')
    monkeypatch.setattr(pipeline.time, 'sleep', unexpected_sleep)
    write_json(tmp_path/'state/run.json', dict(status='running', pid=123))
    write_json(tmp_path/'state/policy-pools.json', dict(status='running', pid=123))
    pipeline.watch(tmp_path, {})
    assert 'policy-pools.json: interrupted' in capsys.readouterr().out
