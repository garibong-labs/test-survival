from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import time

import pytest
import yaml

from test_survival.cli import main
from test_survival.config import STAGES, load, stage_hash, validate
from test_survival.display import DisplayConfigError, UTC_DISPLAY, load_display_settings, settings_path
from test_survival.io import now, read_json, write_json
from test_survival.panel import LiveProgress, panel_lines, watch
from test_survival.pipeline import background, log, run, status
from test_survival.progress import describe, snapshot


@pytest.fixture(autouse=True)
def isolated_user(tmp_path, monkeypatch):
    home = tmp_path/'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)


def configure(text):
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def completed(exp):
    config = validate({'name': 'display', 'anchor': 't', 'project': {'repo': str(exp)}})
    write_json(exp/'state/prepare.json', dict(status='completed',
        started='2026-12-31T17:59:59+00:00', finished='2026-12-31T18:59:59+00:00',
        config_hash=stage_hash(config, 'prepare')))
    return config


def test_missing_settings_default_to_seoul_without_creating_files():
    display = load_display_settings()
    assert settings_path() == Path.home()/'.config/test-survival/config.toml'
    assert not settings_path().exists()
    assert display.timestamp('2026-12-31T18:59:59+00:00') == '2027-01-01T03:59:59+09:00'
    assert now().endswith('+00:00')


@pytest.mark.parametrize('text', ['', '[display]\n'])
def test_empty_settings_keep_default(text):
    configure(text)
    assert load_display_settings().timezone.key == 'Asia/Seoul'


@pytest.mark.parametrize('zone, expected', [
    ('UTC', '2026-01-01T00:30:00+00:00'),
    ('America/New_York', '2025-12-31T19:30:00-05:00'),
    ('Asia/Kathmandu', '2026-01-01T06:15:00+05:45'),
])
def test_custom_time_zone_overrides_default(zone, expected):
    configure(f'[display]\ntimezone = "{zone}"\n')
    assert load_display_settings().timestamp('2026-01-01T00:30:00+00:00') == expected


def test_dst_repeated_clock_time_keeps_distinct_offsets():
    configure('[display]\ntimezone = "America/New_York"')
    display = load_display_settings()
    assert display.timestamp('2026-11-01T05:30:00+00:00') == '2026-11-01T01:30:00-04:00'
    assert display.timestamp('2026-11-01T06:30:00+00:00') == '2026-11-01T01:30:00-05:00'


def test_xdg_replaces_home_path_and_empty_xdg_uses_home(tmp_path, monkeypatch):
    configure('[display]\ntimezone = "UTC"')
    xdg = tmp_path/'user-config'
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg))
    assert settings_path() == xdg/'test-survival/config.toml'
    assert load_display_settings().timezone.key == 'Asia/Seoul'  # No merging with ~/.config.
    configure('[display]\ntimezone = "Asia/Kathmandu"')
    assert load_display_settings().timezone.key == 'Asia/Kathmandu'
    monkeypatch.setenv('XDG_CONFIG_HOME', '')
    assert load_display_settings().timezone.key == 'UTC'


@pytest.mark.parametrize('text', [
    'invalid TOML !', 'other = true', 'display = "UTC"', '[display]\ntypo = "UTC"',
    '[display]\ntimezone = 9', '[display]\ntimezone = ""',
    '[display]\ntimezone = "Mars/Olympus"', '[display]\ntimezone = "../UTC"',
    '[display]\ntimezone = "UTC"\ntimezone = "Asia/Seoul"',
])
def test_bad_settings_fail_before_cli_creates_or_runs_anything(tmp_path, text, capsys):
    configure(text)
    exp = tmp_path/'never-created'
    assert main(['run', str(exp)]) == 2
    assert not exp.exists()
    assert 'survival:' in capsys.readouterr().err
    with pytest.raises(DisplayConfigError):
        load_display_settings()


def test_non_utf8_config_has_a_clear_cli_error(tmp_path, capsys):
    path = configure('')
    path.write_bytes(b'\xff')
    assert main(['status', str(tmp_path/'never-created')]) == 2
    assert 'cannot read valid TOML' in capsys.readouterr().err


def test_relative_xdg_and_unreadable_config_are_errors(monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', 'relative')
    with pytest.raises(DisplayConfigError, match='absolute'):
        load_display_settings()
    monkeypatch.delenv('XDG_CONFIG_HOME')
    settings_path().mkdir(parents=True)
    with pytest.raises(DisplayConfigError, match='cannot read'):
        load_display_settings()


def test_log_status_and_panel_localize_without_changing_evidence(tmp_path, monkeypatch, capsys):
    import test_survival.pipeline as pipeline
    import test_survival.progress as progress
    exp = tmp_path/'experiment'
    config = completed(exp)
    saved = (exp/'state/prepare.json').read_bytes()
    stamp = '2026-12-31T18:59:59.123456+00:00'
    monkeypatch.setattr(pipeline, 'now', lambda: stamp)
    calls = []
    def items(*args):
        calls.append(True)
        return [], 0, 'builds'
    monkeypatch.setattr(progress, 'items', items)
    log(exp, 'prepare: done', config)
    assert len(calls) == 1  # Screen/file representations share a single progress calculation.
    output = capsys.readouterr().out
    assert output.startswith('2027-01-01T03:59:59.123456+09:00')
    assert 'estimated finish 2027-01-01T03:59:59+09:00' in output
    saved_log = (exp/'state/log.txt').read_text()
    assert saved_log.startswith(stamp)
    assert 'estimated finish 2026-12-31T18:59:59+00:00' in saved_log
    assert '+09:00' not in saved_log
    assert '2027-01-01T03:59:59+09:00' in status(exp, config)
    assert '2026-12-31T18:59:59+00:00' in describe(exp, 'prepare', config)
    assert snapshot(exp, 'prepare', config)['eta'] == '2026-12-31T18:59:59+00:00'
    assert '2027-01-01T03:59:59+09:00' in panel_lines(exp, config, 250)[0]
    for width in (80, 45, 30):
        line = panel_lines(exp, config, width)[0]
        assert len(line) < width and '2027-01-01T03:59+09:00' in line
    assert panel_lines(exp, config, 20)[0] == 'ETA hidden'
    assert (exp/'state/prepare.json').read_bytes() == saved


def test_running_eta_crosses_local_midnight_with_unchanged_elapsed(tmp_path, monkeypatch, capsys):
    import test_survival.progress as progress
    import test_survival.pipeline as pipeline
    at = datetime(2026, 12, 31, 14, 59, 30, tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at
    monkeypatch.setattr(progress, 'datetime', Clock)
    monkeypatch.setattr(pipeline, 'now', lambda: at.isoformat())
    exp = tmp_path/'experiment'
    config = completed(exp)
    write_json(exp/'state/prepare.json', {'status': 'running', 'started': '2026-12-31T14:58:30+00:00'})
    evidence = exp/'completed-item'
    evidence.write_text('synthetic evidence')
    os.utime(evidence, (at.timestamp(), at.timestamp()))
    monkeypatch.setattr(progress, 'items', lambda *a: ([[evidence]], 2, 'builds'))
    monkeypatch.setattr(progress, 'panel_items', lambda *a: ([[evidence]], 2, 'builds', {}))
    log(exp, 'prepare: build ok', config)
    shown = capsys.readouterr().out
    assert shown.startswith('2026-12-31T23:59:30+09:00')
    assert 'elapsed 00:01:00; 1/2 builds; estimated finish 2027-01-01T00:00:30+09:00' in shown
    assert 'estimated finish 2026-12-31T15:00:30+00:00' in (exp/'state/log.txt').read_text()
    assert '2027-01-01T00:00:30+09:00' in status(exp, config)
    assert '2027-01-01T00:00+09:00' in panel_lines(exp, config, 80)[0]
    assert snapshot(exp, 'prepare', config)['remaining'] == 60


def test_live_settings_are_stable_across_threads_and_resume_is_unchanged(tmp_path, capsys):
    exp = tmp_path/'experiment'
    config = validate({'name': 'display', 'anchor': 't', 'project': {'repo': str(exp)}})
    before = {stage: stage_hash(config, stage) for stage in STAGES}
    original_environment = os.environ.copy()
    def handler(exp, config):
        configure('[display]\ntimezone = "UTC"')  # Applies to the next command, not this run.
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda n: log(exp, f'prepare: thread {n}', config), range(2)))
        return 'synthetic work complete'
    assert run(exp, config, until='prepare', handlers={'prepare': handler}, no_panel=True) == ['prepare']
    assert all(line.split(' ', 1)[0].endswith('+09:00') for line in capsys.readouterr().out.splitlines())
    record = read_json(exp/'state/prepare.json')
    assert record['started'].endswith('+00:00') and record['finished'].endswith('+00:00')
    assert record['config_hash'] == before['prepare']
    for stage in STAGES:
        assert stage_hash(config, stage) == before[stage]
    original = (exp/'state/prepare.json').read_bytes()
    assert run(exp, config, until='prepare', handlers={}, no_panel=True) == []
    assert '+00:00' in capsys.readouterr().out
    assert (exp/'state/prepare.json').read_bytes() == original
    assert os.environ == original_environment


def test_cli_status_and_watch_read_new_settings_without_writing_state(tmp_path, capsys):
    exp = tmp_path/'experiment'
    config = completed(exp)
    (exp/'experiment.yaml').write_text(yaml.safe_dump(config))
    original = {p.name: p.read_bytes() for p in (exp/'state').iterdir()}
    assert main(['status', str(exp)]) == 0
    assert '2027-01-01T03:59:59+09:00' in capsys.readouterr().out
    configure('[display]\ntimezone = "UTC"')
    assert main(['status', str(exp), '--watch', '--no-panel']) == 0
    assert '2026-12-31T18:59:59+00:00' in capsys.readouterr().out
    assert {p.name: p.read_bytes() for p in (exp/'state').iterdir()} == original


@pytest.mark.parametrize('zone, stamp, eta', [
    ('Asia/Seoul', '2027-01-01T03:59:59.123456+09:00', '2027-01-01T03:59:59+09:00'),
    ('America/New_York', '2026-12-31T13:59:59.123456-05:00', '2026-12-31T13:59:59-05:00'),
    ('UTC', '2026-12-31T18:59:59.123456+00:00', '2026-12-31T18:59:59+00:00'),
])
def test_status_and_watch_localize_last_log_without_rewriting_it(tmp_path, capsys, zone, stamp, eta):
    exp = tmp_path/'experiment'
    config = completed(exp)
    (exp/'experiment.yaml').write_text(yaml.safe_dump(config))
    saved = ('2026-12-31T18:59:59.123456+00:00 prepare: done | elapsed 01:00:00; '
             '1/1 builds; estimated finish 2026-12-31T18:59:59+00:00\n')
    path = exp/'state/log.txt'
    path.write_text(saved)
    configure(f'[display]\ntimezone = "{zone}"')
    for options in ([], ['--watch', '--no-panel']):
        assert main(['status', str(exp), *options]) == 0
        last = next(line for line in capsys.readouterr().out.splitlines() if line.startswith('Last log:'))
        assert last == (f'Last log: {stamp} prepare: done | elapsed 01:00:00; '
                        f'1/1 builds; estimated finish {eta}')
        assert path.read_bytes() == saved.encode()


@pytest.mark.parametrize('message', [
    'generate: 1/2 methods; 2 calls',
    'import: source dated 2026-12-31T18:59:59+00:00',
    'custom text; estimated finish 2026-12-31T18:59:59+00:00',
    'prepare: done | elapsed 01:00:00; 1/1 builds; estimated finish unknown',
    'prepare: done | elapsed 01:00:00; 1/1 builds; estimated finish invalid',
    'prepare: done | elapsed 01:00:00; 1/1 builds; estimated finish 2026-12-31T18:59:59',
])
def test_last_log_preserves_message_text_and_unknown_or_invalid_eta(message):
    display = load_display_settings()
    assert display.log_line('2026-12-31T18:59:59+00:00 ' + message) == '2027-01-01T03:59:59+09:00 ' + message


@pytest.mark.parametrize('line', ['', 'legacy unstructured log', '2026-12-31T18:59:59 plain naive log',
                                  '2026-12-31T18:59:59+00:00'])
def test_last_log_preserves_legacy_and_incomplete_lines(line):
    assert load_display_settings().log_line(line) == line


@pytest.mark.parametrize('selection', ['generated', 'dev-baseline'])
@pytest.mark.parametrize('replacement', ['[display]\ntimezone = "UTC"', 'invalid TOML !'])
@pytest.mark.parametrize('entry', ['cli', 'api'])
def test_import_uses_startup_display_after_copying(tmp_path, monkeypatch, capsys, selection, replacement, entry):
    import test_survival.importer as importer
    exp, source = tmp_path/'experiment', tmp_path/'archive'
    exp.mkdir()
    config = {'name': 'import-display', 'anchor': 't', 'project': {'repo': str(exp)},
              'generators': [{'name': 'o', 'method': 'ouedraogo', 'impl': 'native'}]}
    (exp/'experiment.yaml').write_text(yaml.safe_dump(config))
    if selection == 'generated':
        write_json(source/'generated/o/round1/funnel.json', {'compiled': 0})
    else:
        (source/'dev').mkdir(parents=True)
        for name in ('dev-methods-all.csv', 'dev-denominator.csv'):
            (source/'dev'/name).write_text('class,method\nT,test\n')
    original = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    path = configure('[display]\ntimezone = "Asia/Seoul"')
    copy = importer.shutil.copy2
    def copy_then_edit_settings(*args, **kwargs):
        result = copy(*args, **kwargs)
        path.write_text(replacement)
        return result
    monkeypatch.setattr(importer.shutil, 'copy2', copy_then_edit_settings)
    if entry == 'cli':
        assert main(['import', str(exp), '--from', str(source), '--what', selection]) == 0
    else:
        importer.import_experiment(exp, source, selection)
    assert capsys.readouterr().out.split(' ', 1)[0].endswith('+09:00')
    assert (exp/'state/log.txt').read_text().split(' ', 1)[0].endswith('+00:00')
    assert read_json(exp/'state/import.json')[selection]['imported'].endswith('+00:00')
    assert original == {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}


def test_direct_import_rejects_invalid_display_before_writing(tmp_path):
    from test_survival.importer import import_experiment
    configure('invalid TOML !')
    with pytest.raises(DisplayConfigError):
        import_experiment(tmp_path/'never-created', tmp_path/'archive', 'generated')
    assert not (tmp_path/'never-created').exists()


def test_heartbeat_is_always_a_utc_record(tmp_path, monkeypatch):
    import test_survival.panel as panel
    exp = tmp_path/'experiment'
    config = validate({'name': 'heartbeat', 'anchor': 't', 'project': {'repo': str(exp)},
                       'generators': [{'name': 'k', 'method': 'ktester', 'tool_path': '/unused'}]})
    write_json(exp/'state/generate.json', {'status': 'running'})
    monkeypatch.setattr(panel, 'now', lambda: '2026-12-31T18:59:59+00:00')
    monkeypatch.setattr(panel, 'generation_progress', lambda *a: dict(completed=[], total=1, responses=0, repair=None))
    monitor = LiveProgress(exp, config, disabled=True)
    monitor.last_log -= 121
    monitor.refresh()
    assert (exp/'state/log.txt').read_text().startswith('2026-12-31T18:59:59+00:00')


def test_real_background_child_keeps_both_logs_utc(tmp_path, monkeypatch):
    # A fully cached synthetic stage: no build, model, or experiment work is invoked.
    exp = tmp_path/'experiment'
    config = completed(exp)
    (exp/'experiment.yaml').write_text(yaml.safe_dump(config))
    config = load(exp)
    write_json(exp/'state/prepare.json', dict(status='completed', started='2026-12-31T17:59:59+00:00',
        finished='2026-12-31T18:59:59+00:00', config_hash=stage_hash(config, 'prepare')))
    original = (exp/'state/prepare.json').read_bytes()
    configure('[display]\ntimezone = "Asia/Seoul"')
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1]/'src'))
    pid, path = background(exp, until='prepare')
    deadline = time.monotonic()+10
    try:
        while time.monotonic() < deadline:
            record = read_json(exp/'state/run.json')
            if record['status'] != 'running':
                break
            time.sleep(.02)
        assert record['status'] == 'completed'
        os.waitpid(pid, 0)
    finally:
        if read_json(exp/'state/run.json')['status'] == 'running':
            os.kill(pid, 9)
            os.waitpid(pid, 0)
    for log_path in (path, exp/'state/log.txt'):
        text = log_path.read_text()
        assert 'already completed; skipped' in text and '+09:00' not in text
        assert 'estimated finish 2026-12-31T18:59:59+00:00' in text
        assert all(line.split(' ', 1)[0].endswith('+00:00') for line in text.splitlines())
    assert record['started'].endswith('+00:00') and record['finished'].endswith('+00:00')
    assert (exp/'state/prepare.json').read_bytes() == original
