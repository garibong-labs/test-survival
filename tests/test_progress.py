from datetime import datetime, timedelta, timezone
import os

import pytest

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import canonical_hash, validate
from test_survival.io import read_json, write_csv, write_json
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


def prepare_plan(exp, config, *, count=3, anchor='anchor', prefix='sha', paths=None):
    write_json(exp/'state/resolved-range.json', dict(end='end', config_hash=canonical_hash(
        {k: config[k] for k in ('project', 'anchor', 'range')})))
    write_json(exp/'results/anchor.json', dict(sha=anchor, tag=config['anchor'], origin_master='end'))
    write_csv(exp/'results/commits.csv', [{'index': i, 'sha': prefix+str(i)} for i in range(1, count+1)], ['index', 'sha'])
    write_json(exp/'results/commit-paths.json', {str(i): paths or ['src/main/java/A.java'] for i in range(1, count+1)})


def build_record(exp, config, index, sha, *, outcome='ok', age=-100):
    adapter = JavaMavenAdapter(exp, config)
    if outcome == 'ok':
        (adapter.folder(index)/'target/classes').mkdir(parents=True, exist_ok=True)
    record(exp/f'results/builds/{index}.json', dict(index=index, sha=sha, build=outcome,
           input_sha256=adapter.build_fingerprint(sha)), age=age)


def test_prepare_counts_completed_builds_not_highest_index_and_excludes_cache(tmp_path, config):
    state(tmp_path, 'prepare')
    prepare_plan(tmp_path, config)
    build_record(tmp_path, config, 0, 'anchor')
    build_record(tmp_path, config, 3, 'sha3', outcome='fail', age=50)
    text = describe(tmp_path, 'prepare', config, at=AT)
    assert 'elapsed 00:01:40; 2/4 builds' in text
    assert 'estimated finish 2026-01-01T00:05:00+00:00' in text
    build_record(tmp_path, config, 3, 'sha3', outcome='fail', age=-100)
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
    adapter = JavaMavenAdapter(tmp_path, config)
    record(root/'validation/0.json', dict(file='generated/o/round1/sources/A.java',
           input_sha256=adapter.screen_fingerprint(root/'sources/A.java', config['screen']['stable_runs'])))
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
    prepare_plan(tmp_path, config)
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
    # Progress follows run_group(): a record from a supported historical
    # provenance counts as done; a coverage-fail record never does.
    from test_survival.adapters.run_reuse import PRE_MULTI_RELEASE
    historical = {**adapter.provenance(), **PRE_MULTI_RELEASE}
    record(path, dict(status='evaluated', sha='anchor', input_sha256=adapter.run_fingerprint([]), runner_sha256=historical))
    assert '1/1 indices' in describe(tmp_path, 'cover', config, at=AT)
    record(path, dict(status='coverage-fail', sha='anchor', input_sha256=adapter.run_fingerprint([]), runner_sha256=adapter.provenance()))
    assert '0/1 indices' in describe(tmp_path, 'cover', config, at=AT)


@pytest.mark.parametrize('change', ['runner', 'module', 'range', 'classes'])
def test_prepare_rejects_invalidated_builds_and_keeps_real_cache_hits(tmp_path, config, monkeypatch, change):
    import shutil
    state(tmp_path, 'prepare')
    prepare_plan(tmp_path, config, count=2)
    adapter = JavaMavenAdapter(tmp_path, config)
    shas = {0: 'anchor', 1: 'sha1', 2: 'sha2'}
    for index, sha in shas.items():
        build_record(tmp_path, config, index, sha)
    assert '3/3 builds' in describe(tmp_path, 'prepare', config, at=AT)
    def unexpected_build(*args):
        pytest.fail('Reusable build was unexpectedly executed')
    monkeypatch.setattr(adapter, 'checkout', unexpected_build)
    assert all(adapter.build(index, sha)['sha'] == sha for index, sha in shas.items())
    if change == 'runner':
        config['runner']['timeouts']['build'] += 1
    elif change == 'module':
        config['project']['module'] = 'module'
    elif change == 'range':
        config['range']['end'] = 'new-end'
        shas.update({1: 'new-sha1', 2: 'new-sha2'})
    else:
        for index in shas:
            shutil.rmtree(adapter.folder(index)/'target/classes')
    prepare_plan(tmp_path, config, count=2, prefix='new-sha' if change == 'range' else 'sha')
    cached = 1 if change == 'range' else 0
    assert f'{cached}/3 builds; estimated finish unknown' in describe(tmp_path, 'prepare', config, at=AT)
    # The real adapter must reject the same stale record.
    class RebuildRequired(Exception):
        pass
    def rebuilding(*args):
        raise RebuildRequired
    monkeypatch.setattr(adapter, 'checkout', rebuilding)
    with pytest.raises(RebuildRequired):
        adapter.build(1, shas[1])
    build_record(tmp_path, config, 1, shas[1], age=50)
    text = describe(tmp_path, 'prepare', config, at=AT)
    assert f'{cached+1}/3 builds' in text
    finish = '00:03:20' if cached else '00:05:00'
    assert f'estimated finish 2026-01-01T{finish}+00:00' in text


def test_prepare_waits_for_current_range_outputs_and_validates_anchor(tmp_path, config):
    state(tmp_path, 'prepare')
    prepare_plan(tmp_path, config, count=1)
    build_record(tmp_path, config, 0, 'anchor')
    build_record(tmp_path, config, 1, 'sha1')
    config['anchor'] = 'new-anchor-tag'
    assert '0/? builds' in describe(tmp_path, 'prepare', config, at=AT)
    # Resolving a new range alone must not bless the previous anchor/index files.
    write_json(tmp_path/'state/resolved-range.json', dict(end='end', config_hash=canonical_hash(
        {k: config[k] for k in ('project', 'anchor', 'range')})))
    assert '0/? builds' in describe(tmp_path, 'prepare', config, at=AT)
    prepare_plan(tmp_path, config, count=1, anchor='new-anchor')
    assert '1/2 builds' in describe(tmp_path, 'prepare', config, at=AT)


def test_prepare_counts_new_failures_but_retries_old_failures(tmp_path, config):
    state(tmp_path, 'prepare')
    prepare_plan(tmp_path, config, count=1)
    build_record(tmp_path, config, 0, 'anchor')
    build_record(tmp_path, config, 1, 'sha1', outcome='fail')
    assert '1/2 builds' in describe(tmp_path, 'prepare', config, at=AT)
    build_record(tmp_path, config, 1, 'sha1', outcome='fail', age=50)
    assert '2/2 builds' in describe(tmp_path, 'prepare', config, at=AT)
    # A resumed prepare will retry the failure, even with unchanged inputs.
    write_json(tmp_path/'state/prepare.json', dict(status='running', started=AT.isoformat()))
    assert '1/2 builds' in describe(tmp_path, 'prepare', config, at=AT+timedelta(seconds=100))


@pytest.mark.parametrize('change', ['skip_rule', 'paths', 'sha'])
def test_prepare_validates_skipped_build_decisions(tmp_path, config, change):
    config['range']['skip_untouched'] = 'main-sources'
    state(tmp_path, 'prepare')
    prepare_plan(tmp_path, config, count=1, paths=['README.md'])
    build_record(tmp_path, config, 0, 'anchor')
    record(tmp_path/'results/builds/1.json', dict(sha='sha1', build='skipped-untouched',
           reason='No changes under src/main/ or to any pom.xml'), age=-100)
    assert '2/2 builds' in describe(tmp_path, 'prepare', config, at=AT)
    if change == 'skip_rule':
        config['range']['skip_untouched'] = 'none'
    prepare_plan(tmp_path, config, count=1, prefix='new-sha' if change == 'sha' else 'sha',
                 paths=['src/main/java/A.java'] if change == 'paths' else ['README.md'])
    assert '1/2 builds' in describe(tmp_path, 'prepare', config, at=AT)


@pytest.mark.parametrize('method', ['ouedraogo', 'ktester'])
@pytest.mark.parametrize('change', ['repeats', 'source', 'provenance'])
def test_screen_rejects_stale_validation_like_the_adapter(tmp_path, config, monkeypatch, method, change):
    config['generators'][0]['method'] = method
    state(tmp_path, 'screen')
    root = tmp_path/'generated/o/round1'
    folder = root/'project/test_classes' if method == 'ktester' else root/'sources'
    folder.mkdir(parents=True)
    adapter = JavaMavenAdapter(tmp_path, config)
    sources = [folder/f'{name}.java' for name in ('A', 'B')]
    for index, source in enumerate(sources):
        source.write_text('class '+source.stem+' {}')
        record(root/f'validation/{index}.json', dict(file=str(source.relative_to(tmp_path)),
               input_sha256=adapter.screen_fingerprint(source, config['screen']['stable_runs'])), age=-100)
    assert '2/2 files' in describe(tmp_path, 'screen', config, at=AT)
    calls = []
    def screen(job):
        calls.append(job)
        return {'file': job['file']}
    monkeypatch.setattr(adapter, 'worker', screen)
    adapter.screen_file('o', 'round1', sources[0], config['screen']['stable_runs'], 0)
    assert not calls
    if change == 'repeats':
        config['screen']['stable_runs'] += 1
    elif change == 'source':
        for source in sources:
            source.write_text('class '+source.stem+' { void changed() {} }')
    else:
        monkeypatch.setattr(JavaMavenAdapter, 'provenance', lambda self: {'runner': 'changed'})
    assert '0/2 files; estimated finish unknown' in describe(tmp_path, 'screen', config, at=AT)
    result = adapter.screen_file('o', 'round1', sources[0], config['screen']['stable_runs'], 0)
    assert len(calls) == 1
    record(root/'validation/0.json', result, age=50)
    text = describe(tmp_path, 'screen', config, at=AT)
    assert '1/2 files' in text
    assert 'estimated finish 2026-01-01T00:03:20+00:00' in text
