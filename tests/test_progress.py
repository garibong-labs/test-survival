from datetime import datetime, timedelta, timezone
import os

import pytest

from test_survival.config import validate
from test_survival.io import write_csv, write_json
from test_survival.pipeline import log, status
from test_survival.progress import describe


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
AT = START + timedelta(seconds=100)


@pytest.fixture
def config():
    return validate({'name': 'progress', 'project': {'repo': '/unused'}, 'anchor': 't',
                     'generators': [{'name': 'o', 'method': 'ouedraogo', 'impl': 'native',
                                     'params': {'prompts': ['ZSL', 'FSL']}}]})


def state(exp, stage, **extra):
    write_json(exp/f'state/{stage}.json', dict(status='running', started=START.isoformat(), **extra))


def record(path, value, age=50):
    write_json(path, value)
    timestamp = (START+timedelta(seconds=age)).timestamp()
    os.utime(path, (timestamp, timestamp))


def commits(exp):
    write_csv(exp/'results/commits.csv', [{'index': i} for i in range(1, 4)], ['index'])


def test_prepare_counts_completed_builds_not_highest_index_and_excludes_cache(tmp_path, config):
    state(tmp_path, 'prepare')
    commits(tmp_path)
    record(tmp_path/'results/builds/0.json', {'build': 'ok'}, age=-100)
    record(tmp_path/'results/builds/3.json', {'build': 'fail'})
    text = describe(tmp_path, 'prepare', config, at=AT)
    assert 'elapsed 00:01:40; 2/4 builds' in text
    assert 'estimated finish 2026-01-01T00:05:00+00:00' in text
    record(tmp_path/'results/builds/3.json', {'build': 'fail'}, age=-100)
    assert 'estimated finish unknown' in describe(tmp_path, 'prepare', config, at=AT)


def test_generate_counts_call_ledgers_retries_and_early_stop(tmp_path, config):
    state(tmp_path, 'generate', group='o')
    write_json(tmp_path/'population/scope.json', {'classes': [{'class': 'A'}, {'class': 'B'}]})
    root = tmp_path/'generated/o/round1'
    record(root/'runs/A/ZSL/run.json', {'status': 'http-error'})
    record(root/'runs/A/ZSL/attempt-2/run.json', {'status': 'ok'})
    record(root/'runs/A/FSL/request.json', {})
    text = describe(tmp_path, 'generate', config, at=AT)
    assert '2/5 calls' in text
    assert 'estimated finish 2026-01-01T00:04:10+00:00' in text
    write_json(tmp_path/'generated/o/rounds.json', {'stopped': 'zero-compile'})
    assert '2/2 calls' in describe(tmp_path, 'generate', config, at=AT)


def test_ktester_live_calls_are_not_double_counted_after_archival(tmp_path, config):
    config['generators'][0]['method'] = 'ktester'
    state(tmp_path, 'generate')
    for base in ('work/ktester/o/rounds', 'generated/o'):
        record(tmp_path/base/'round1/raw-responses/0001/call.json', {'status': 'ok'})
    assert '1/? calls; estimated finish unknown' in describe(tmp_path, 'generate', config, at=AT)
    write_json(tmp_path/'generated/o/rounds.json', {'stopped': 'max-rounds'})
    assert '1/1 calls' in describe(tmp_path, 'generate', config, at=AT)


def test_screen_uses_files_and_its_own_elapsed_time(tmp_path, config):
    state(tmp_path, 'screen')
    root = tmp_path/'generated/o/round1'
    for name in ('A', 'B'):
        path = root/f'sources/{name}.java'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('class '+name+' {}')
    record(root/'validation/0.json', {'file': 'generated/o/round1/sources/A.java'})
    record(tmp_path/'results/raw/99-dev.json', {})
    text = describe(tmp_path, 'screen', config, at=AT)
    assert '1/2 files' in text
    assert 'estimated finish 2026-01-01T00:03:20+00:00' in text


@pytest.mark.parametrize('stage,unit', [('survive', 'commits'), ('cover', 'indices')])
def test_commit_progress_waits_for_all_groups_and_handles_failed_builds(tmp_path, config, stage, unit):
    state(tmp_path, stage)
    commits(tmp_path)
    write_csv(tmp_path/'results/coverage-commits.csv', [{'index': i} for i in range(1, 4)], ['index'])
    write_json(tmp_path/'results/frozen-denominators.json', {'dev': [], 'o': []})
    for i in (1, 2, 3):
        record(tmp_path/f'results/builds/{i}.json', {'build': 'fail' if i == 2 else 'ok', 'sha': str(i)}, age=-100)
    from test_survival.adapters.java_maven import JavaMavenAdapter
    adapter = JavaMavenAdapter(tmp_path, config)
    write_json(tmp_path/'results/alive-entering.json', {str(i): {'dev': [], 'o': []} for i in (1, 2, 3)})
    for i, group in ((1, 'dev'), (1, 'o'), (3, 'dev')):
        folder = 'raw' if stage == 'survive' else 'raw-coverage'
        record(tmp_path/f'results/{folder}/{i}-{group}.json', dict(status='evaluated', sha=str(i),
               input_sha256=adapter.run_fingerprint([]), runner_sha256=adapter.provenance()))
        if stage == 'cover':
            path = tmp_path/f'results/coverage/{i}-{group}-lines.csv'
            write_csv(path, [], ['file', 'line', 'covered'])
            os.utime(path, (AT.timestamp(), AT.timestamp()))
    text = describe(tmp_path, stage, config, at=AT)
    assert f'2/3 {unit}' in text
    assert 'estimated finish 2026-01-01T00:03:20+00:00' in text


def test_status_and_log_share_progress_and_completed_elapsed_is_frozen(tmp_path, config, capsys):
    state(tmp_path, 'prepare')
    commits(tmp_path)
    log(tmp_path, 'prepare: build recorded', config)
    assert '0/4 builds; estimated finish unknown' in capsys.readouterr().out
    assert '0/4 builds; estimated finish unknown' in status(tmp_path, config)
    write_json(tmp_path/'state/prepare.json', {'status': 'completed', 'started': START.isoformat(), 'finished': AT.isoformat()})
    assert 'elapsed 00:01:40' in describe(tmp_path, 'prepare', config)
    assert 'estimated finish 2026-01-01T00:01:40+00:00' in describe(tmp_path, 'prepare', config)


def test_fresh_experiment_has_unknown_totals_without_writes(tmp_path, config):
    for stage in ('prepare', 'survive', 'cover'):
        assert '0/?' in describe(tmp_path, stage, config, at=AT)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('stage', ['survive', 'cover'])
@pytest.mark.parametrize('change', ['survival', 'source', 'methods', 'provenance', 'commit'])
def test_invalidated_records_do_not_count_as_done(tmp_path, config, stage, change):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    state(tmp_path, stage)
    adapter = JavaMavenAdapter(tmp_path, config)
    (tmp_path/'T.java').write_text('class T {}')
    methods = [{'id': 'one', 'file': 'T.java'}, {'id': 'two', 'file': 'T.java'}]
    write_json(tmp_path/'results/frozen-denominators.json', {'dev': methods})
    write_json(tmp_path/'results/alive-entering.json', {str(i): {'dev': ['one', 'two']} for i in (1, 2)})
    for filename in ('commits.csv', 'coverage-commits.csv'):
        write_csv(tmp_path/'results'/filename, [{'index': i} for i in (1, 2)], ['index'])
    folder = 'raw' if stage == 'survive' else 'raw-coverage'
    for i in (1, 2):
        write_json(tmp_path/f'results/builds/{i}.json', {'build': 'ok', 'sha': str(i)})
        record(tmp_path/f'results/{folder}/{i}-dev.json', dict(status='evaluated', sha=str(i),
               input_sha256=adapter.run_fingerprint(methods), runner_sha256=adapter.provenance()), age=-100)
        write_csv(tmp_path/f'results/coverage/{i}-dev-lines.csv', [], ['file', 'line', 'covered'])
    assert '2/2' in describe(tmp_path, stage, config, at=AT)
    if change == 'survival':
        config['survival']['flaky_rerun'] += 1
    elif change == 'source':
        (tmp_path/'T.java').write_text('class T { void changed() {} }')
    elif change == 'methods':
        methods = methods[:1]
        write_json(tmp_path/'results/frozen-denominators.json', {'dev': methods})
    elif change == 'provenance':
        for i in (1, 2):
            from test_survival.io import read_json
            path = tmp_path/f'results/{folder}/{i}-dev.json'
            raw = read_json(path)
            raw['runner_sha256'] = {'old': 'code'}
            write_json(path, raw)
    else:
        for i in (1, 2):
            write_json(tmp_path/f'results/builds/{i}.json', {'build': 'ok', 'sha': 'new'+str(i)})
    assert '0/2' in describe(tmp_path, stage, config, at=AT)
    # Only the first result is refreshed. The stale second result must not
    # make the estimate jump to "now" after this first completion.
    record(tmp_path/f'results/{folder}/1-dev.json', dict(status='evaluated', sha='new1' if change == 'commit' else '1',
           input_sha256=adapter.run_fingerprint(methods), runner_sha256=adapter.provenance()))
    text = describe(tmp_path, stage, config, at=AT)
    assert '1/2' in text
    assert 'estimated finish 2026-01-01T00:03:20+00:00' in text


def test_coverage_progress_validates_alive_entering_methods(tmp_path, config):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    state(tmp_path, 'cover')
    (tmp_path/'T.java').write_text('class T {}')
    methods = [{'id': 'one', 'file': 'T.java'}]
    adapter = JavaMavenAdapter(tmp_path, config)
    write_json(tmp_path/'results/frozen-denominators.json', {'dev': methods})
    write_json(tmp_path/'results/alive-entering.json', {'0': {'dev': []}})
    write_json(tmp_path/'results/anchor.json', {'sha': 'anchor'})
    write_csv(tmp_path/'results/coverage-commits.csv', [{'index': 0}], ['index'])
    write_csv(tmp_path/'results/coverage/0-dev-lines.csv', [], ['file', 'line', 'covered'])
    path = tmp_path/'results/raw-coverage/0-dev.json'
    record(path, dict(status='evaluated', sha='anchor', input_sha256=adapter.run_fingerprint(methods), runner_sha256=adapter.provenance()))
    assert '0/1 indices' in describe(tmp_path, 'cover', config, at=AT)
    record(path, dict(status='evaluated', sha='anchor', input_sha256=adapter.run_fingerprint([]), runner_sha256=adapter.provenance()))
    assert '1/1 indices' in describe(tmp_path, 'cover', config, at=AT)
