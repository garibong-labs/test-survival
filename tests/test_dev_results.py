from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from test_survival.config import load
from test_survival.dev_results import file_hash
from test_survival.importer import import_experiment
from test_survival.io import read_csv, read_json, write_csv, write_json
from test_survival.pipeline import PipelineError
from test_survival.rules.dev_denominator import denominator_file, frozen_method
from test_survival.stages import control, cover, screen, survive


@pytest.fixture
def experiments(tmp_path, monkeypatch):
    source, target = tmp_path/'old', tmp_path/'new'
    raw = dict(name='weekly', project={'repo': str(tmp_path/'repo')}, anchor='t',
               range={'skip_untouched': 'main-sources'},
               dev={'groups': [{'name': 'dev-all', 'denominator': 'project'},
                               {'name': 'dev-scope', 'denominator': 'coverage-scoped', 'derive_from': 'dev-all'},
                               {'name': 'local', 'denominator': 'scope-targeted'}]},
               generators=[{'name': 'llm', 'method': 'ouedraogo', 'impl': 'native'}])
    methods = [frozen_method({'class': 'p.T', 'method': m, 'kind': 'plain'}) for m in ('one', 'two')]
    commits = [dict(index=i, sha=f'sha{i}', date='2026-01-01', subject='change',
                    **{'changes-src-main': 'no' if i == 2 else 'yes', 'changes-pom': 'no'}) for i in (1, 2, 3)]
    for exp in (source, target):
        exp.mkdir()
        settings = deepcopy(raw)
        if exp == target:
            settings['dev']['groups'] = [settings['dev']['groups'][1], settings['dev']['groups'][0],
                                         {'name': 'local', 'denominator': 'project'}]
        (exp/'experiment.yaml').write_text(yaml.safe_dump(settings))
        config = load(exp)
        path = exp/'dev/src-test-t/java/p/T.java'
        path.parent.mkdir(parents=True)
        path.write_text('package p; class T { void one() {} void two() {} }')
        write_csv(exp/'dev/dev-methods-all.csv', [{'class': 'p.T', 'method': m, 'kind': 'plain', 'stable': 'yes'} for m in ('one', 'two')], ['class', 'method', 'kind', 'stable'])
        frozen = {'dev-all': methods, 'dev-scope': methods[1:], 'local': methods[:1], 'llm': []}
        write_json(exp/'results/frozen-denominators.json', frozen)
        write_json(exp/'results/denominators.json', {g: len(ms) for g, ms in frozen.items()})
        for i, group in enumerate(config['dev']['groups']):
            write_csv(exp/denominator_file(group, i), [{k: r[k] for k in ('class', 'method', 'kind')} for r in frozen[group['name']]], ['class', 'method', 'kind'])
        write_json(exp/'results/anchor.json', {'sha': 'anchor', 'date': '2026-01-01'})
        write_csv(exp/'results/commits.csv', commits, list(commits[0]))
        write_json(exp/'results/commit-paths.json', {'1': ['src/main/java/A.java'], '2': ['README.md'], '3': ['src/main/java/A.java']})
        for i in (1, 2, 3):
            write_json(exp/f'results/builds/{i}.json', {'sha': f'sha{i}', 'build': 'ok'})
        write_json(exp/'population/scope.json', {'classes': [{'file': 'src/main/java/A.java'}]})
    calls = []
    class Adapter:
        def __init__(self, exp, config):
            self.exp, self.runner = exp, self
        def setup_runner(self):
            calls.append(('setup',))
        def session(self, stage):
            return nullcontext()
        def run_group(self, index, group, rows, phase='A'):
            calls.append((phase, index, group))
            methods = deepcopy(rows)
            for row in methods:
                row.update(state='fail' if index == 3 and row['method'] == 'two' else 'pass', flaky=False)
            result = dict(index=index, group=group, sha='anchor' if index == 0 else f'sha{index}',
                          status='evaluated', methods=methods, phase=phase, input_sha256='archived-fingerprint',
                          runner_sha256='archived-runner', files=[{'compile': {'exit_code': 0}, 'runs': [], 'methods': methods}])
            folder = 'control' if phase == 'control' else 'raw'
            write_json(self.exp/f'results/{folder}/{index}-{group}.json', result)
            return result
        def git(self, *args):
            return 'parent' if args[0] == 'rev-parse' else '--- a/src/main/java/A.java\n+++ b/src/main/java/A.java\n@@ -1 +1 @@\n-old\n+new'
        def coverage(self, index, files, rows, group):
            calls.append(('B', index, group))
            write_csv(self.exp/f'results/coverage/{index}-{group}-lines.csv',
                      [{'file': 'src/main/java/A.java', 'line': 1, 'covered': 'yes' if rows else 'no'}], ['file', 'line', 'covered'])
            (self.exp/f'results/coverage/{index}-{group}.xml').write_text('<coverage/>')
            result = dict(status='evaluated', methods=rows, sha='anchor' if index == 0 else f'sha{index}',
                          input_sha256='archived-fingerprint', runner_sha256='archived-runner')
            write_json(self.exp/f'results/raw-coverage/{index}-{group}.json', result)
            return result
    for stage in (control, survive, cover, screen):
        monkeypatch.setattr(stage, 'JavaMavenAdapter', Adapter)
    for stage in (control, survive, cover):
        stage.run(source, load(source))
    calls.clear()
    return source, target, calls


def test_reuses_matching_dev_groups_and_reports_their_source(experiments):
    from test_survival.report.consistency import build
    from test_survival.progress import snapshot
    source, target, calls = experiments
    config = load(target)
    original = {str(p.relative_to(source)): file_hash(p) for p in source.rglob('*') if p.is_file()}
    import_experiment(target, source, 'dev-results')
    provenance = read_json(target/'state/import.json')['dev-results']
    assert set(provenance['groups']) == {'dev-all', 'dev-scope'}
    assert all('local' not in path and 'llm' not in path for path in provenance['files'])
    assert read_csv(target/'dev/dev-denominator.csv')[0]['method'] == 'two'
    assert len(read_csv(target/'dev/dev-all-denominator.csv')) == 2
    assert len(read_csv(target/'dev/local-denominator.csv')) == 1
    assert provenance['source'] == str(source)
    assert provenance['files']['results/raw-derived/1-dev-scope.json']
    assert provenance['files']['results/coverage/0-dev-all.xml']
    for stage in (control, survive, cover):
        stage.run(target, config)
    executed = [call for call in calls if call[0] != 'setup']
    assert {call[2] for call in executed} == {'llm', 'local'}
    assert {call[0] for call in executed} == {'A', 'B', 'control'}
    assert all(file_hash(target/path) == digest for path, digest in provenance['files'].items())
    source_curve = read_csv(source/'results/survival.csv')
    target_curve = read_csv(target/'results/survival.csv')
    for group in ('dev-all', 'dev-scope'):
        assert [r[group+'_alive'] for r in source_curve] == [r[group+'_alive'] for r in target_curve]
    assert read_json(target/'results/survival-consistency.json')['raw_count_matches']
    summary = build(target, config)
    assert summary['imported_dev_results']['source'] == str(source)
    assert summary['imported_dev_results']['groups'] == ['dev-all', 'dev-scope']
    assert summary['final_survivors']['dev-all'] == 1
    assert summary['final_survivors']['dev-scope'] == 0
    # Imported evidence is counted even with its original runner fingerprint.
    assert snapshot(target, 'control', config)['done'] >= 3
    assert original == {str(p.relative_to(source)): file_hash(p) for p in source.rglob('*') if p.is_file()}


@pytest.mark.parametrize('change, message', [('repo', 'project repo'), ('anchor', 'anchor sha'),
    ('commits', 'commit sha sequence'), ('skip', 'skip_untouched'), ('tree', 'tree hash'), ('rename', 'tree hash')])
def test_refuses_incompatible_experiments_before_copying(experiments, change, message):
    source, target, _ = experiments
    config = yaml.safe_load((target/'experiment.yaml').read_text())
    if change == 'repo':
        config['project']['repo'] = '/different'
    elif change == 'anchor':
        write_json(target/'results/anchor.json', {'sha': 'other'})
    elif change == 'commits':
        rows = read_csv(target/'results/commits.csv')
        rows[0]['sha'], rows[2]['sha'] = rows[2]['sha'], rows[0]['sha']
        write_csv(target/'results/commits.csv', rows, list(rows[0]))
    elif change == 'skip':
        config['range']['skip_untouched'] = 'none'
    elif change == 'tree':
        (target/'dev/src-test-t/java/p/T.java').write_text('changed')
    else:
        (target/'dev/src-test-t/java/p/T.java').rename(target/'dev/src-test-t/java/p/Other.java')
    (target/'experiment.yaml').write_text(yaml.safe_dump(config))
    before = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob('*') if p.is_file()}
    with pytest.raises(PipelineError, match=message):
        import_experiment(target, source, 'dev-results')
    assert all((target/path).read_bytes() == content for path, content in before.items())
    assert not (target/'results/raw').exists()
    assert not (target/'state/import.json').exists()


def test_no_matching_groups_and_missing_source_control_are_clear(experiments):
    source, target, _ = experiments
    (source/'results/control/0-dev-all.json').unlink()
    with pytest.raises(PipelineError, match='missing control result for dev-all'):
        import_experiment(target, source, 'dev-results')
    settings = yaml.safe_load((target/'experiment.yaml').read_text())
    settings['dev']['groups'] = [{'name': 'different', 'denominator': 'project'}]
    (target/'experiment.yaml').write_text(yaml.safe_dump(settings))
    with pytest.raises(PipelineError, match='no Dev groups'):
        import_experiment(target, source, 'dev-results')


@pytest.mark.parametrize('change, message', [('tree', 'tree hash'), ('record', 'missing or changed'),
                                            ('missing', 'missing or changed'), ('frozen', 'frozen denominator')])
def test_invalidated_import_cannot_silently_skip_measurement(experiments, change, message):
    source, target, calls = experiments
    import_experiment(target, source, 'dev-results')
    if change == 'tree':
        (target/'dev/src-test-t/java/p/T.java').write_text('different')
    elif change == 'record':
        write_json(target/'results/raw/1-dev-all.json', {'methods': []})
    elif change == 'missing':
        (target/'results/raw/1-dev-all.json').unlink()
    else:
        frozen = read_json(target/'results/frozen-denominators.json')
        frozen['dev-all'] = []
        write_json(target/'results/frozen-denominators.json', frozen)
    with pytest.raises(PipelineError, match=message):
        survive.run(target, load(target))
    assert calls == []


def test_screen_keeps_imported_frozen_population(experiments, monkeypatch):
    source, target, _ = experiments
    import_experiment(target, source, 'dev-results')
    original = read_json(target/'results/frozen-denominators.json')['dev-all']
    write_csv(target/'dev/dev-all-denominator.csv', [], ['class', 'method', 'kind'])
    screen.run(target, load(target))
    assert read_json(target/'results/frozen-denominators.json')['dev-all'] == original


def test_imported_groups_need_no_runner_when_they_are_the_only_groups(experiments, monkeypatch):
    source, target, calls = experiments
    settings = yaml.safe_load((target/'experiment.yaml').read_text())
    settings['generators'] = []
    settings['dev']['groups'] = [g for g in settings['dev']['groups'] if g['name'] != 'local']
    (target/'experiment.yaml').write_text(yaml.safe_dump(settings))
    frozen = read_json(target/'results/frozen-denominators.json')
    write_json(target/'results/frozen-denominators.json', {g: frozen[g] for g in ('dev-all', 'dev-scope')})
    import_experiment(target, source, 'dev-results')
    for stage in (control, survive, cover):
        stage.run(target, load(target))
    assert calls == []


def test_scope_preserves_imported_populations_without_remeasuring_coverage(experiments, monkeypatch):
    from test_survival.stages import scope
    source, target, _ = experiments
    import_experiment(target, source, 'dev-results')
    write_json(target/'results/source-inventory.json', {
        'main': {'classes': {'p.A': {'path': 'src/main/java/A.java'}},
                 'methods': [{'class': 'p.A', 'method': 'f', 'signature': 'f()', 'line': 1, 'eligible': True}]},
        'test': {'classes': {'p.T': {'path': 'src/test/java/p/T.java'}}}})
    class Adapter:
        def __init__(self, *args):
            self.runner, self.helpers = self, target/'helpers'
        def folder(self, index):
            return target
        def execute(self, command, cwd, log, timeout):
            log.write_text('class\tmethod\taccessor\np.A\tf\tfalse\n')
            return {'exit_code': 0}
        def setup_runner(self):
            pytest.fail('Imported coverage-scoped population was remeasured')
    monkeypatch.setattr(scope, 'JavaMavenAdapter', Adapter)
    monkeypatch.setattr(scope, 'select_scope', lambda survey, config: (survey, len(survey)))
    config = load(target)
    config['scope']['min_classes'] = 1
    scope.run(target, config)
    assert len(read_csv(target/'dev/dev-all-denominator.csv')) == 2
    assert [r['method'] for r in read_csv(target/'dev/dev-denominator.csv')] == ['two']


def test_import_survives_source_removal_and_changed_derived_execution(experiments):
    source, target, calls = experiments
    settings = yaml.safe_load((target/'experiment.yaml').read_text())
    settings['dev']['groups'][0].pop('derive_from')
    (target/'experiment.yaml').write_text(yaml.safe_dump(settings))
    import_experiment(target, source, 'dev-results')
    assert (target/'results/raw/1-dev-scope.json').read_bytes() == (source/'results/raw-derived/1-dev-scope.json').read_bytes()
    source.rename(source.with_name('archived-elsewhere'))
    for stage in (control, survive, cover):
        stage.run(target, load(target))
    assert all(call[2] in ('llm', 'local') for call in calls if len(call) == 3)
    assert read_json(target/'results/survival-consistency.json')['raw_count_matches']


def test_destination_build_failure_does_not_count_unused_imports_as_measured(experiments):
    source, target, _ = experiments
    write_json(target/'results/builds/3.json', {'sha': 'sha3', 'build': 'fail'})
    import_experiment(target, source, 'dev-results')
    survive.run(target, load(target))
    assert read_json(target/'results/survival-consistency.json')['raw_count_matches']
    assert read_csv(target/'results/survival.csv')[-1]['dev-all_alive'] == '2'
    cover.run(target, load(target))
    unavailable = read_json(target/'results/coverage-consistency.json')['missing']
    assert {r['group'] for r in unavailable if r['index'] == 3} == {'dev-all', 'dev-scope', 'local', 'llm'}


def test_missing_required_imported_coverage_fails_instead_of_silently_skipping(experiments):
    source, target, _ = experiments
    (source/'results/raw-coverage/3-dev-all.json').unlink()
    import_experiment(target, source, 'dev-results')
    survive.run(target, load(target))
    with pytest.raises(PipelineError, match='raw-coverage/3-dev-all.json'):
        cover.run(target, load(target))


def test_stale_source_denominator_cannot_overwrite_an_unmatched_group(experiments):
    source, target, _ = experiments
    settings = yaml.safe_load((source/'experiment.yaml').read_text())
    settings['dev']['groups'] = [g for g in settings['dev']['groups'] if g['name'] != 'local']
    (source/'experiment.yaml').write_text(yaml.safe_dump(settings))
    write_csv(source/'dev/local-denominator.csv', [], ['class', 'method', 'kind'])
    original = (target/'dev/local-denominator.csv').read_bytes()
    import_experiment(target, source, 'dev-results')
    assert (target/'dev/local-denominator.csv').read_bytes() == original


@pytest.mark.parametrize('section,keys,value', [
    ('runner', ('docker', 'image'), 'jdk:21'),
    ('runner', ('timeouts', 'test_class'), 60),
    ('runner', ('jvm', 'launcher'), '2g'),
    ('survival', ('flaky_rerun',), 0),
    ('survival', ('environment_errors', 'patterns'), ['java.net.BindException']),
    ('project', ('module',), 'other-module'),
    ('project', ('test_framework',), 'junit4'),
    ('dev', ('stable_runs',), 5),
    ('coverage', ('at_anchor',), False),
    ('coverage', ('changed_line',), False),
])
def test_result_affecting_settings_refuse_import_before_copy(experiments, section, keys, value):
    source, target, _ = experiments
    settings = yaml.safe_load((target/'experiment.yaml').read_text())
    parent = settings.setdefault(section, {})
    for key in keys[:-1]:
        parent = parent.setdefault(key, {})
    parent[keys[-1]] = value
    (target/'experiment.yaml').write_text(yaml.safe_dump(settings))
    with pytest.raises(PipelineError, match='result-affecting measurement settings'):
        import_experiment(target, source, 'dev-results')
    assert not (target/'results/raw').exists()
    assert not (target/'state/import.json').exists()


def test_operational_settings_and_obsolete_generation_options_allow_import(experiments):
    source, target, _ = experiments
    settings = yaml.safe_load((source/'experiment.yaml').read_text())
    settings['generators'] = [{'obsolete_generator': True, 'rounds': {'retry_on_zero_compile': 1}}]
    settings['survival'] = {'dev_cache': 'none'}
    settings['obsolete_report_option'] = True
    (source/'experiment.yaml').write_text(yaml.safe_dump(settings))
    target_settings = yaml.safe_load((target/'experiment.yaml').read_text())
    target_settings['runner'] = {'workers': {'survive': 1}, 'reuse': {'checkouts': '/cache'},
                                 'docker': {'mounts': {'/host': '/repo'}, 'env_file': '/private/docker.env'}}
    (target/'experiment.yaml').write_text(yaml.safe_dump(target_settings))
    import_experiment(target, source, 'dev-results')
    assert set(read_json(target/'state/import.json')['dev-results']['groups']) == {'dev-all', 'dev-scope'}


def test_unknown_archived_measurement_setting_is_not_silently_ignored(experiments):
    source, target, _ = experiments
    settings = yaml.safe_load((source/'experiment.yaml').read_text())
    settings['runner'] = {'unknown_measurement_option': 'unsafe-to-guess'}
    (source/'experiment.yaml').write_text(yaml.safe_dump(settings))
    with pytest.raises(PipelineError, match='source compatibility configuration'):
        import_experiment(target, source, 'dev-results')
    assert not (target/'results/raw').exists()


def test_import_hashes_copied_bytes_and_retains_identical_tree(experiments, monkeypatch):
    import test_survival.dev_results as module
    source, target, _ = experiments
    changed = 'results/raw/1-dev-all.json'
    tree_path = target/'dev/src-test-t/java/p/T.java'
    before = tree_path.stat()
    original_copy = module.shutil.copy2
    copies = []
    def copy(src, dst):
        copies.append(str(src))
        assert 'src-test-t' not in str(src)
        if src == source/changed:
            record = read_json(src)
            record['late_observation'] = True
            write_json(src, record)
        return original_copy(src, dst)
    monkeypatch.setattr(module.shutil, 'copy2', copy)
    import_experiment(target, source, 'dev-results')
    record = read_json(target/'state/import.json')['dev-results']
    assert record['files'][changed] == file_hash(target/changed)
    assert read_json(target/changed)['late_observation']
    after = tree_path.stat()
    assert (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) == (after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
    assert copies
    assert read_json(target/'state/prepare.json')['dev_baseline']['source'] == str(source)


def test_digest_cache_avoids_repeated_reads_and_detects_same_size_changes(tmp_path, monkeypatch):
    import os
    import test_survival.dev_results as module
    path = tmp_path/'result.json'
    path.write_text('AAAA')
    module._file_hash.cache_clear()
    original_read = Path.read_bytes
    reads = []
    def read(self):
        reads.append(self)
        return original_read(self)
    monkeypatch.setattr(Path, 'read_bytes', read)
    old = module.file_hash(path)
    stat = path.stat()
    assert module.file_hash(path) == old
    assert len(reads) == 1
    path.write_text('BBBB')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert module.file_hash(path) != old  # ctime changed even with the old mtime.
    assert len(reads) == 2
    replacement = tmp_path/'replacement'
    replacement.write_text('CCCC')
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replacement.replace(path)
    module.file_hash(path)
    assert len(reads) == 3


def test_repeated_validation_and_progress_use_cached_digests(experiments, monkeypatch):
    import test_survival.dev_results as module
    source, target, _ = experiments
    import_experiment(target, source, 'dev-results')
    module._file_hash.cache_clear()
    reads = []
    original_read = Path.read_bytes
    def read(self):
        reads.append(self)
        return original_read(self)
    monkeypatch.setattr(Path, 'read_bytes', read)
    provenance = module.load_provenance(target)
    groups = module.imported_groups(target, load(target), 'survive', provenance=provenance)
    initial_reads = len(reads)
    assert initial_reads > 0
    for _ in range(2):
        module.imported_groups(target, load(target), 'survive', provenance=provenance)
        for group in groups:
            folder = 'raw-derived' if group == 'dev-scope' else 'raw'
            relative = f'results/{folder}/1-{group}.json'
            module.imported_record(target, relative, provenance=provenance)
            assert module.imported_file_valid(target, relative, provenance['files'])
    assert len(reads) == initial_reads


def test_import_invalidates_scope_and_rebuilds_llm_subsampling_and_funnels(experiments, monkeypatch):
    from test_survival.config import STAGES, stage_hash
    source, target, _ = experiments
    settings = yaml.safe_load((target/'experiment.yaml').read_text())
    settings['screen'] = {'matching': 'subsample-to-dev'}
    (target/'experiment.yaml').write_text(yaml.safe_dump(settings))
    config = load(target)
    for stage in STAGES:
        write_json(target/f'state/{stage}.json', {'status': 'completed', 'config_hash': stage_hash(config, stage)})
    write_csv(target/'dev/dev-denominator.csv', [], ['class', 'method', 'kind'])
    root = target/'generated/llm/round1'
    java = root/'sources/ZSL/L.java'
    java.parent.mkdir(parents=True)
    java.write_text('class L {}')
    write_json(root/'funnel.json', {'developer_denominator': 0, 'ratio_to_dev': None})
    def screen_file(self, group, round_name, source_file, repeats, number):
        methods = [dict(file=str(source_file.relative_to(target)), **{'class': 'L'}, method=m,
                        kind='plain', id=m, body_hash=m, round='round1', technique='ZSL', state='pass') for m in ('one', 'two')]
        return {'compile': {'exit_code': 0}, 'methods': methods, 'file': str(source_file.relative_to(target))}
    monkeypatch.setattr(screen.JavaMavenAdapter, 'screen_file', screen_file, raising=False)
    import_experiment(target, source, 'dev-results')
    assert read_json(target/'state/prepare.json')['status'] == 'completed'
    assert all(read_json(target/f'state/{s}.json')['status'] == 'pending' for s in STAGES[1:])
    screen.run(target, config)
    assert len(read_json(target/'results/frozen-denominators.json')['llm']) == 1
    funnel = read_json(root/'funnel.json')
    assert funnel['developer_denominator'] == 1 and funnel['ratio_to_dev'] == 2


def test_cover_runner_lifecycle_uses_submitted_jobs_even_with_extra_frozen_group(experiments):
    import shutil
    source, target, calls = experiments
    settings = yaml.safe_load((target/'experiment.yaml').read_text())
    settings['generators'] = []
    settings['dev']['groups'] = [g for g in settings['dev']['groups'] if g['name'] != 'local']
    (target/'experiment.yaml').write_text(yaml.safe_dump(settings))
    frozen = read_json(target/'results/frozen-denominators.json')
    frozen.pop('local')
    write_json(target/'results/frozen-denominators.json', frozen)
    import_experiment(target, source, 'dev-results')
    for name in ('alive-entering.json', 'survival.csv'):
        shutil.copy2(source/'results'/name, target/'results'/name)
    cover.run(target, load(target))
    assert calls[0] == ('setup',)
    assert [(phase, group) for phase, _, group in calls[1:]] == [('B', 'llm')]*3


def test_cover_does_not_start_runner_when_no_measurement_jobs_exist(experiments):
    import shutil
    source, target, calls = experiments
    settings = yaml.safe_load((target/'experiment.yaml').read_text())
    settings['coverage'] = {'at_anchor': False, 'changed_line': False}
    (target/'experiment.yaml').write_text(yaml.safe_dump(settings))
    for index in (1, 3):
        write_json(target/f'results/builds/{index}.json', {'build': 'fail'})
    for name in ('alive-entering.json', 'survival.csv'):
        shutil.copy2(source/'results'/name, target/'results'/name)
    cover.run(target, load(target))
    assert calls == []


def test_imported_baseline_validation_preserves_source_stability_set(experiments):
    from test_survival.dev_results import imported_baseline
    source, target, _ = experiments
    import_experiment(target, source, 'dev-results')
    assert imported_baseline(target, load(target))
    write_csv(target/'dev/dev-methods-all.csv', [], ['class', 'method', 'stable'])
    with pytest.raises(PipelineError, match='dev-methods-all.csv'):
        imported_baseline(target, load(target))
