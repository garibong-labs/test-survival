from types import SimpleNamespace

import pytest

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import validate
from test_survival.io import atomic_text, read_csv, read_json, write_csv, write_json
from test_survival.stages import screen
from test_survival.stages.generated_exclusions import apply_exclusions


def fixture(exp, *, ktester=False, selectors=()):
    group = dict(name='g', method='ktester' if ktester else 'ouedraogo',
                 impl='author-tool' if ktester else 'native', **({'tool_path':'/unused'} if ktester else {}))
    config = validate(dict(name='test', project={'repo':'/unused'}, anchor='t', generators=[group],
                           screen={'exclude':{'g':list(selectors)}}))
    group = config['generators'][0]
    write_csv(exp/'dev/dev-denominator.csv', [], ['class', 'method', 'kind'])
    adapter = JavaMavenAdapter(exp, config)
    rows = []
    for number, (name, body, state) in enumerate([('A', 'same', 'pass'), ('B', 'same', 'pass'),
                                                ('C', 'other', 'pass'), ('D', 'failed', 'fail')]):
        directory = exp/'generated/g'/f'round{number+1}'
        source = directory/('project/test_classes' if ktester else 'sources')/(name+'.java')
        atomic_text(source, 'class '+name+' {}')
        file = str(source.relative_to(exp))
        row = dict(file=file, **{'class':'p.'+name}, method='works', kind='plain',
                   body_hash=body, state=state, id=file+'|p.'+name+'|works',
                   round=directory.name, technique='KTester' if ktester else 'ZSL')
        write_json(directory/'validation/0.json', dict(file=file, methods=[row], compile={'exit_code':0},
                    input_sha256=adapter.screen_fingerprint(source, config['screen']['stable_runs'])))
        rows.append(row)
    return config, group, rows


@pytest.mark.parametrize('selector', ['p.A', 'p.A#works', 'generated/g/round1/sources/A.java'])
def test_cached_screen_filters_after_cross_round_dedup_and_audits(tmp_path, monkeypatch, selector):
    config, _, rows = fixture(tmp_path, selectors=[selector, 'p.Missing'])
    records = list((tmp_path/'generated').glob('*/round*/validation/*.json'))
    before = {p:p.read_bytes() for p in records}
    def unexpected(*args, **kwargs):
        pytest.fail('Fingerprint-matching screening records must not execute tests')
    monkeypatch.setattr(JavaMavenAdapter, 'setup_runner', lambda *a: None)
    monkeypatch.setattr(JavaMavenAdapter, 'worker', unexpected)
    screen.run(tmp_path, config)
    frozen = read_json(tmp_path/'results/frozen-denominators.json')['g']
    assert [t['class'] for t in frozen] == ['p.C']  # B is not promoted after excluding A.
    audit = read_csv(tmp_path/'results/generated-exclusions.csv')
    assert audit == [dict(group='g', file=rows[0]['file'], **{'class':'p.A'}, method='works',
                          selector=selector, screening_state='pass', prior_admission='yes',
                          reason='excluded-by-configuration')]
    summary = read_json(tmp_path/'results/generated-exclusions.json')['g']
    assert (summary['before'], summary['excluded'], summary['after'], summary['unmatched']) == (2, 1, 1, ['p.Missing'])
    for n, final in [(1, 0), (2, 0), (3, 1), (4, 1)]:
        funnel = read_json(tmp_path/f'generated/g/round{n}/funnel.json')
        assert funnel['final_kept'] == final and funnel['configured_excluded_total'] == 1
    assert {p:p.read_bytes() for p in records} == before
    screen.run(tmp_path, config)
    assert read_csv(tmp_path/'results/generated-exclusions.csv') == audit
    config['screen']['exclude'] = {}
    screen.run(tmp_path, config)
    assert [t['class'] for t in read_json(tmp_path/'results/frozen-denominators.json')['g']] == ['p.A', 'p.C']
    assert read_csv(tmp_path/'results/generated-exclusions.csv') == []


def test_ktester_alias_and_method_selector_keep_sibling(tmp_path):
    config, group, rows = fixture(tmp_path, ktester=True, selectors=['generated/g/round1/sources/A.java'])
    assert apply_exclusions(tmp_path, config, group, rows[:1]) == []
    sibling = dict(rows[0], method='other', id='other')
    config['screen']['exclude'] = {'g':['p.A#works']}
    assert apply_exclusions(tmp_path, config, group, [rows[0], sibling]) == [sibling]
    config['screen']['exclude'] = {'g':['generated/g/round1/other/test_classes/A.java']}
    assert apply_exclusions(tmp_path, config, group, rows[:1]) == rows[:1]


@pytest.mark.parametrize('damage', ['fresh', 'missing', 'source', 'repeats'])
def test_uncached_or_changed_file_is_screened_with_exclusions(tmp_path, monkeypatch, damage):
    config, _, rows = fixture(tmp_path, selectors=['p.A'])
    record = tmp_path/'generated/g/round1/validation/0.json'
    if damage == 'fresh':
        for path in (tmp_path/'generated').glob('*/round*/validation/*.json'):
            path.unlink()
    elif damage == 'missing': record.unlink()
    elif damage == 'source': (tmp_path/'generated/g/round1/sources/A.java').write_text('changed')
    elif damage == 'repeats': config['screen']['stable_runs'] += 1
    calls = []
    def worker(self, job):
        calls.append(job['file'])
        return dict(file=job['file'], methods=[next(r for r in rows if r['file'] == job['file'])],
                    compile={'exit_code':0})
    monkeypatch.setattr(JavaMavenAdapter, 'setup_runner', lambda *a: None)
    monkeypatch.setattr(JavaMavenAdapter, 'worker', worker)
    screen.run(tmp_path, config)
    assert set(calls) == {r['file'] for r in (rows if damage in ('fresh', 'repeats') else rows[:1])}
    assert [t['class'] for t in read_json(tmp_path/'results/frozen-denominators.json')['g']] == ['p.C']
    assert read_csv(tmp_path/'results/generated-exclusions.csv')[0]['class'] == 'p.A'


def test_audit_discloses_failed_candidates_without_counting_them_as_removed(tmp_path):
    config, group, rows = fixture(tmp_path, selectors=['p.D'])
    assert apply_exclusions(tmp_path, config, group, rows[:1]) == rows[:1]
    audit = read_csv(tmp_path/'results/generated-exclusions.csv')
    assert audit[0]['screening_state'] == 'fail' and audit[0]['prior_admission'] == 'no'
    assert read_json(tmp_path/'results/generated-exclusions.json')['g']['excluded'] == 0


@pytest.mark.parametrize('control_excluded', [False, True])
@pytest.mark.parametrize('cached', [True, False])
def test_generated_policy_pool_screens_then_filters(tmp_path, monkeypatch, cached, control_excluded):
    from test_survival.policy import pools
    pool_exp = tmp_path/'results/policy/pools/g/2'
    config, _, rows = fixture(pool_exp, selectors=['p.A'])
    if not cached:
        (pool_exp/'generated/g/round1/validation/0.json').unlink()
    scope = [dict(file='src/main/java/Production.java', **{'class':'p.Production'})]
    for row in rows:
        write_json(pool_exp/'generated/g'/row['round']/'generation.json',
                   [dict(file=row['file'], target_class='p.Production')])
    b = SimpleNamespace(exp=tmp_path, execution_config=config, scope={'classes':scope},
                        control_exclusions={'g':{('p.C', 'works')}} if control_excluded else {},
                        by_index={2:{'date':'2026-01-01'}})
    write_json(tmp_path/'state/run.json', dict(status='running', run_id='fixture'))
    monkeypatch.setattr(JavaMavenAdapter, 'build', lambda *a: {'build':'ok'})
    monkeypatch.setattr(JavaMavenAdapter, 'setup_runner', lambda *a: None)
    monkeypatch.setattr(JavaMavenAdapter, 'list_production_methods',
                        lambda *a: dict(classes={'p.Production':{}}, methods=[]))
    monkeypatch.setattr(JavaMavenAdapter, 'list_test_methods', lambda *a: {'methods':[]})
    calls = []
    def worker(self, job):
        calls.append(job['file'])
        assert not cached and job['file'] == rows[0]['file']
        return dict(file=job['file'], methods=rows[:1], compile={'exit_code':0})
    monkeypatch.setattr(JavaMavenAdapter, 'worker', worker)
    monkeypatch.setattr(pools, 'generate', lambda *a, **kw:pytest.fail('No model calls'))
    pool = pools.build_pool(b, dict(group='g', index=2, sha='sha'), 0)
    assert [t['class'] for t in pool] == ([] if control_excluded else ['p.C'])
    assert read_csv(pool_exp/'results/generated-exclusions.csv')[0]['class'] == 'p.A'
    assert read_json(pool_exp/'funnel.json')[-1]['final_kept'] == 1
    assert calls == ([] if cached else [rows[0]['file']])


def test_report_discloses_exclusions_and_unmatched_selectors(tmp_path, monkeypatch):
    from test_survival.stages import report
    config, _, _ = fixture(tmp_path, selectors=['p.A', 'p.Missing'])
    monkeypatch.setattr(JavaMavenAdapter, 'setup_runner', lambda *a: None)
    screen.run(tmp_path, config)
    write_csv(tmp_path/'results/survival.csv', [dict(index=0, dev_alive=0, g_alive=1)],
              ['index', 'dev_alive', 'g_alive'])
    write_json(tmp_path/'results/survival-consistency.json',
               dict(missing_records=[], nonincreasing=True, raw_count_matches=True))
    write_json(tmp_path/'results/coverage-consistency.json',
               dict(identical_executable_line_sets=[], coverage_indices=[], missing=[]))
    monkeypatch.setattr(report.tables, 'build', lambda *a: None)
    monkeypatch.setattr(report.figures, 'build', lambda *a: None)
    monkeypatch.setattr(report.figures, 'png', lambda *a: False)
    from test_survival.generators import completion
    monkeypatch.setattr(completion, 'record', lambda *a: None)
    report.run(tmp_path, config)
    summary = read_json(tmp_path/'results/final-summary.json')
    assert summary['generated_exclusions']['g']['excluded'] == 1
    for path in ['README.md', 'results/consistency.md']:
        text = (tmp_path/path).read_text()
        assert 'screen.exclude removed 1 of 2' in text
        assert 'unmatched screen.exclude selectors: p.Missing' in text
