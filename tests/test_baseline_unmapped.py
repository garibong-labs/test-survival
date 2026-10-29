from pathlib import Path
import shutil

import pytest

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import validate
from test_survival.io import read_csv, read_json, write_csv, write_json
from test_survival.rules.dev_denominator import coverage_scoped, denominator


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    config = validate({'name': 'unmapped', 'anchor': 't', 'project': {'repo': str(tmp_path)}})
    adapter = JavaMavenAdapter(tmp_path, config)
    source = adapter.folder(0)/'src/test/java/fixture/OuterTest.java'
    source.parent.mkdir(parents=True)
    shutil.copy2(Path(__file__).parent/'fixtures/unmapped/OuterTest.java', source)
    outer, nested = 'fixture.OuterTest', 'fixture.OuterTest$NestedTest'
    # The syntax-only inventory attributes inherited() to its declaring outer
    # class; Surefire reports its execution under the concrete nested class.
    methods = [dict(zip(('class', 'method'), pair), kind='plain', display='')
               for pair in [(outer, 'inherited'), (nested, 'mapped'), (nested, 'failing')]]
    monkeypatch.setattr(adapter, 'list_production_methods', lambda _: {'classes': {}, 'methods': []})
    monkeypatch.setattr(adapter, 'list_test_methods', lambda _: {'classes': {}, 'methods': methods})
    for run in range(1, 4):
        write_json(tmp_path/f'results/baseline-run{run}.json', {'index': run})
        report = tmp_path/f'results/baseline-reports/run{run}/TEST-{nested}.xml'
        report.parent.mkdir(parents=True)
        report.write_text('<testsuite><testcase name="inherited()"/>'
                          '<testcase name="mapped()"/>'
                          '<testcase name="failing()"><failure/></testcase></testsuite>')
    return adapter, nested


def test_nested_inherited_baseline_defaults_to_error(baseline):
    adapter, nested = baseline
    with pytest.raises(RuntimeError, match='Baseline cases cannot be mapped unambiguously'):
        adapter.baseline()
    assert read_json(adapter.exp/'results/baseline-unmapped.json') == [
        {'run': run, 'class': nested, 'name': 'inherited()'} for run in range(1, 4)]
    assert not (adapter.exp/'dev/dev-methods-all.csv').exists()
    assert read_csv(adapter.exp/'dev/dev-unmapped-excluded.csv') == []


def test_nested_inherited_cases_excluded_from_every_dev_denominator(baseline):
    adapter, nested = baseline
    adapter.config['dev']['unmapped'] = 'exclude'
    rows = adapter.baseline()
    assert read_csv(adapter.exp/'dev/dev-unmapped-excluded.csv') == [
        {'class': nested, 'name': 'inherited()', 'runs_seen': '3', 'reason': 'no-source-method'}]
    assert [(r['method'], r['stable'], r['run1'], r['run3']) for r in rows] == [
        ('inherited', 'no', 'absent', 'absent'), ('mapped', 'yes', 'pass', 'pass'),
        ('failing', 'no', 'fail', 'fail')]
    expected = [{'class': nested, 'method': 'mapped', 'kind': 'plain'}]
    assert denominator(rows, 'project', [], set()) == expected
    assert denominator(rows, 'scope-targeted', [{'class': 'fixture.Outer'}], set()) == expected
    assert coverage_scoped(rows, [{**expected[0], 'status': 'pass', 'scope_lines_covered': 1}]) == (expected, [])
    # Removing only the unmapped cases produces exactly the same mapped baseline
    # under the default policy, and clears the old exclusion audit on rerun.
    for report in (adapter.exp/'results/baseline-reports').rglob('TEST-*.xml'):
        report.write_text(report.read_text().replace('<testcase name="inherited()"/>', ''))
    adapter.config['dev']['unmapped'] = 'error'
    assert adapter.baseline() == rows
    assert read_csv(adapter.exp/'dev/dev-unmapped-excluded.csv') == []


def test_ambiguous_and_skipped_cases_audited_once_per_case_and_run(baseline):
    adapter, nested = baseline
    adapter.config['dev']['unmapped'] = 'exclude'
    methods = adapter.list_test_methods(None)['methods']
    methods[1]['display'] = methods[2]['display'] = 'same display'
    report = adapter.exp/f'results/baseline-reports/run1/TEST-{nested}.xml'
    report.write_text(report.read_text().replace('</testsuite>',
        '<testcase name="same display"/><testcase name="same display"/>'
        '<testcase name="unknown"><skipped/></testcase></testsuite>'))
    rows = adapter.baseline()
    excluded = {r['name']: r for r in read_csv(adapter.exp/'dev/dev-unmapped-excluded.csv')}
    assert excluded['same display']['reason'] == 'ambiguous-source-method'
    assert excluded['same display']['runs_seen'] == '1'
    assert excluded['unknown']['reason'] == 'no-source-method'
    assert excluded['unknown']['runs_seen'] == '1'
    assert next(r for r in rows if r['method'] == 'mapped')['stable'] == 'yes'


def test_exclusion_count_in_funnel_and_report(tmp_path, monkeypatch):
    from test_survival.stages import screen, report
    config = validate({'name': 'notes', 'anchor': 't', 'project': {'repo': str(tmp_path)},
                       'generators': [{'name': 'llm', 'method': 'ouedraogo', 'impl': 'native'}]})
    write_json(tmp_path/'results/anchor.json', {'dev_unmapped_excluded': 2})
    write_csv(tmp_path/'dev/dev-denominator.csv', [], ['class', 'method', 'kind'])
    directory = tmp_path/'generated/llm/round1'
    _, funnel = screen.screen_round(tmp_path, config, config['generators'][0], directory)
    assert funnel['dev_unmapped_excluded'] == 2
    assert '2 unmapped baseline cases' in funnel['notes'][0]
    assert 'dev/dev-unmapped-excluded.csv' in (directory/'funnel.md').read_text()
    write_json(tmp_path/'results/denominators.json', {'dev': 0, 'llm': 0})
    write_csv(tmp_path/'results/survival.csv', [{'index': 0, 'dev_alive': 0, 'llm_alive': 0}],
              ['index', 'dev_alive', 'llm_alive'])
    write_json(tmp_path/'results/survival-consistency.json',
               {'missing_records': [], 'nonincreasing': True, 'raw_count_matches': True})
    write_json(tmp_path/'results/coverage-consistency.json',
               {'identical_executable_line_sets': [], 'coverage_indices': [], 'missing': []})
    monkeypatch.setattr(report.tables, 'build', lambda *args: None)
    monkeypatch.setattr(report.figures, 'build', lambda *args: None)
    monkeypatch.setattr(report.figures, 'png', lambda *args: False)
    from test_survival.generators import completion
    monkeypatch.setattr(completion, 'record', lambda *args: None)
    report.run(tmp_path, config)
    summary = read_json(tmp_path/'results/final-summary.json')
    assert summary['dev_unmapped_excluded'] == 2
    for path in ('README.md', 'results/consistency.md'):
        assert summary['notes'][0] in (tmp_path/path).read_text()
