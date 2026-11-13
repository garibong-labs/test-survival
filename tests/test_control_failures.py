"""Admission after control, using fixtures only (no Java, Docker or model calls)."""
from contextlib import nullcontext
from copy import deepcopy

import pytest
import yaml

from test_batch_baseline import fixture, setup_worker
from test_policy import baseline
from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import ConfigError, SCHEMA, STAGES, as_written, stage_hash, survival_inputs, validate
from test_survival.denominators import denominator_counts, finish_control, frozen_after_control
from test_survival.io import read_csv, read_json, write_csv, write_json
from test_survival.stages import batch, control, cover, report, survive


@pytest.mark.parametrize('execution', ['legacy', 'batch'])
def test_optional_config_preserves_old_validation_and_hashes(monkeypatch, execution):
    raw = dict(name='fixture', anchor='t', project={'repo':'/repo'}, survival={'execution':execution})
    with monkeypatch.context() as patch:
        schema = dict(SCHEMA['survival']); schema.pop('control_failures')
        patch.setitem(SCHEMA, 'survival', schema)
        old = validate(raw)
    current = validate(raw)
    assert current == old
    assert 'control_failures' not in as_written(current)['survival']
    assert [stage_hash(current, s) for s in STAGES] == [stage_hash(old, s) for s in STAGES]
    for mode in ('stop', 'exclude'):
        new = validate({**raw, 'survival':{**raw['survival'], 'control_failures':mode}})
        assert [stage_hash(current, s) != stage_hash(new, s) for s in STAGES] == [False]*5+[True]*3
        assert survival_inputs(new) == survival_inputs(current)


@pytest.mark.parametrize('value', ['ignore', '', None, True, 1, []])
def test_invalid_control_failure_mode(value):
    with pytest.raises(ConfigError, match='survival.control_failures'):
        validate(dict(name='fixture', anchor='t', project={'repo':'/repo'}, survival={'control_failures':value}))


def test_helper_is_group_specific_and_requires_both_config_and_artifact(tmp_path):
    c = fixture(tmp_path)
    frozen = read_json(tmp_path/'results/frozen-denominators.json')
    original = (tmp_path/'results/frozen-denominators.json').read_bytes()
    c['survival']['control_failures'] = 'exclude'
    assert frozen_after_control(tmp_path, c) == frozen
    excluded = frozen['dev'][1]['id']
    write_json(tmp_path/'results/control-excluded.json', {'dev':[excluded], 'child':[], 'o':[]})
    assert len(frozen_after_control(tmp_path, c)['dev']) == 1
    assert frozen_after_control(tmp_path, c)['child'] == frozen['child']
    assert denominator_counts(tmp_path, c) == {'dev':1, 'child':1, 'o':1}
    c['survival']['control_failures'] = 'stop'
    assert frozen_after_control(tmp_path, c) == frozen
    assert (tmp_path/'results/frozen-denominators.json').read_bytes() == original


def install_legacy(monkeypatch, jobs):
    monkeypatch.setattr(JavaMavenAdapter, 'setup_runner', lambda *a:None)
    monkeypatch.setattr(survive, 'measurement_session', lambda *a:nullcontext())
    def run(self, index, group, rows, phase='A', **kwargs):
        jobs.append(dict(index=index, group=group, methods=deepcopy(rows)))
        members = [dict(t, state='fail' if phase=='control' and t['method']=='a' else 'pass',
                        message='failure first line\ntraceback', flaky=False) for t in rows]
        raw = dict(index=index, sha='t' if index==0 else str(index), group=group,
                   status='evaluated', phase=phase, methods=members, files=[],
                   input_sha256=self.run_fingerprint(rows), runner_sha256=self.provenance())
        write_json(self.exp/f'results/{"control" if phase=="control" else "raw"}/{index}-{group}.json', raw)
        return raw
    def coverage(self, index, files, rows, group):
        jobs.append(dict(index=index, group=group, methods=deepcopy(rows), coverage=True))
        write_csv(self.exp/f'results/coverage/{index}-{group}-lines.csv',
                  [dict(file='src/main/java/A.java', line=1, covered='yes' if rows else 'no')],
                  ['file','line','covered'])
        return dict(status='evaluated')
    monkeypatch.setattr(JavaMavenAdapter, 'run_group', run)
    monkeypatch.setattr(JavaMavenAdapter, 'coverage', coverage)


@pytest.mark.parametrize('execution', ['legacy', 'batch'])
@pytest.mark.parametrize('mode', [None, 'stop', 'exclude'])
def test_control_and_downstream_populations(tmp_path, monkeypatch, execution, mode):
    c = fixture(tmp_path); c['survival']['execution'] = execution
    if mode is not None: c['survival']['control_failures'] = mode
    jobs = []; setup_worker(monkeypatch, jobs)
    if execution == 'legacy':
        install_legacy(monkeypatch, jobs)
    else:
        worker = JavaMavenAdapter.worker
        def fail_at_anchor(self, job):
            raw = worker(self, job)
            if job['index']==0:
                for t in raw['methods']:
                    if t['method']=='a':
                        t.update(state='error', message='failure first line\ntraceback')
                        t.pop('lines')  # A failed measurement need not have usable coverage.
            return raw
        monkeypatch.setattr(JavaMavenAdapter, 'worker', fail_at_anchor)
    original = (tmp_path/'results/frozen-denominators.json').read_bytes()
    if mode != 'exclude':
        with pytest.raises(RuntimeError, match='Frozen methods failed the control at t'):
            control.run(tmp_path, c)
        assert not (tmp_path/'results/control-excluded.json').exists()
        return
    control.run(tmp_path, c)
    frozen = read_json(tmp_path/'results/frozen-denominators.json')
    expected = {'dev':[frozen['dev'][0]['id']], 'child':[], 'o':[]}
    assert read_json(tmp_path/'results/control-excluded.json') == expected
    assert read_csv(tmp_path/'results/control-excluded.csv')[0]['message'] == 'failure first line'
    assert set(read_csv(tmp_path/'results/control-excluded.csv')[0]) == {'group','class','method','file','state','message'}
    assert read_json(tmp_path/'results/control-at-t.json')['dev']['requested'] == 2
    assert 'excluded after control at t: 1; denominator used: 1' in (tmp_path/'results/control-at-t.md').read_text()
    assert denominator_counts(tmp_path, c) == {'dev':1, 'child':1, 'o':1}
    if execution == 'batch':
        # Simulate the exact pre-change implementation fingerprint in a failed control cache.
        m = batch.Measurements(tmp_path, c, 'control')
        raw = read_json(tmp_path/'results/baseline-batches/0.json')
        raw['input_sha256'] = next(iter(m.control_digests-{m.digest}))
        write_json(tmp_path/'results/baseline-batches/0.json', raw, compress=True)
        before = len(jobs)
        control.run(tmp_path, c)
        assert len(jobs) == before
        assert batch.stage_complete(tmp_path, c, 'control')
        assert read_json(tmp_path/'results/control-excluded.json') == expected
    jobs.clear()
    survive.run(tmp_path, c); cover.run(tmp_path, c)
    assert all(t['method']!='a' for job in jobs for t in job['methods'])
    assert all(frozen['dev'][0]['id'] not in ids for groups in read_json(tmp_path/'results/alive-entering.json').values()
               for ids in groups.values())
    assert all(r['dev_alive']=='1' for r in read_csv(tmp_path/'results/survival.csv'))
    assert all(r['method']!='a' for r in read_csv(tmp_path/'results/first-failures.csv'))
    assert (tmp_path/'results/frozen-denominators.json').read_bytes() == original
    from test_survival.progress import items, panel_items
    assert len(items(tmp_path, 'survive', c, {})[0]) == 6
    # Control progress represents every measured screen method, not the reduced population.
    assert panel_items(tmp_path, 'control', c, {})[1] == 4
    monkeypatch.setattr(report.figures, 'png', lambda *a:False)
    from test_survival.generators import completion
    monkeypatch.setattr(completion, 'record', lambda *a:None)
    report.run(tmp_path, c)
    summary = read_json(tmp_path/'results/final-summary.json')
    assert summary['denominators'] == {'dev':1, 'child':1, 'o':1}
    assert summary['control_excluded'] == {'dev':1, 'child':0, 'o':0}
    for path in ('README.md', 'results/consistency.md'):
        assert 'excluded after control at t: 1; denominator used: 1' in (tmp_path/path).read_text()
    assert 'n=1' in (tmp_path/'results/figure1-survival-by-commit.svg').read_text()


def test_policy_baseline_filters_before_validation_and_snapshots(baseline):
    from test_survival.policy.baseline import Baseline
    from test_survival.policy.execute import baseline_tests
    exp, c, root = baseline
    path = root.parent/'baseline.yaml'
    config = yaml.safe_load(path.read_text()); config['survival'] = {'control_failures':'exclude'}
    path.write_text(yaml.safe_dump(config))
    frozen = read_json(root/'results/frozen-denominators.json')
    removed = dict(frozen['dev-scope'][0], id='excluded', method='broken')
    frozen['dev-scope'].append(removed)
    write_json(root/'results/frozen-denominators.json', frozen)
    write_json(root/'results/control-excluded.json', {'dev-scope':['excluded']})
    before = {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    b = Baseline(exp, c)
    assert [t['id'] for t in b.frozen['dev-scope']] == ['test-id']
    assert b.control_exclusions == {'dev-scope':{('ATest','broken')}}
    assert [t['method'] for t in baseline_tests(b)['dev-scope']] == ['works']
    assert before == {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_empty_and_all_failed_groups_are_audited(tmp_path):
    c = fixture(tmp_path); c['survival']['control_failures'] = 'exclude'
    frozen = read_json(tmp_path/'results/frozen-denominators.json'); frozen['o'] = []
    write_json(tmp_path/'results/frozen-denominators.json', frozen)
    records = {g:dict(requested=len(ts), passed=0, failures=[dict(t, state='absent', message='') for t in ts])
               for g, ts in frozen.items()}
    finish_control(tmp_path, c, records)
    assert denominator_counts(tmp_path, c) == {'dev':0, 'child':0, 'o':0}
    assert read_json(tmp_path/'results/control-excluded.json')['o'] == []
    assert 'excluded after control at t: 0; denominator used: 0' in (tmp_path/'results/control-at-t.md').read_text()


from test_dev_results import experiments as legacy_experiments
from test_batch_dev_results import experiments as batch_experiments


@pytest.mark.parametrize('execution', ['legacy', 'batch'])
def test_dev_result_import_exports_filtered_rows_and_keeps_control_evidence(request, monkeypatch, execution):
    from test_survival.config import load
    from test_survival.dev_results import import_results, imported_groups
    if execution == 'legacy':
        source, target, _ = request.getfixturevalue('legacy_experiments')
        removed_method = 'one'
        monkeypatch.setattr(survive, 'cached_survival_record', lambda *a:None)
    else:
        source, target, _ = request.getfixturevalue('batch_experiments')
        removed_method = 'a'
    for exp in (source, target):
        path = exp/'experiment.yaml'
        config = yaml.safe_load(path.read_text())
        config.setdefault('survival', {})['control_failures'] = 'exclude'
        path.write_text(yaml.safe_dump(config))
    c = load(source)
    original = (source/'results/frozen-denominators.json').read_bytes()
    records = {}
    for g, rows in read_json(source/'results/frozen-denominators.json').items():
        raw_path = source/f'results/control/0-{g}.json'
        raw = read_json(raw_path)
        for t in raw['methods']:
            if t['method'] == removed_method: t.update(state='fail', message='anchor failure')
        write_json(raw_path, raw)
        records[g] = dict(requested=len(rows), passed=sum(t['state']=='pass' for t in raw['methods']),
                          failures=[t for t in raw['methods'] if t['state']!='pass'])
    finish_control(source, c, records)
    survive.run(source, c); cover.run(source, c)
    target_config = load(target)
    provenance = import_results(target, source, target_config)
    write_json(target/'state/import.json', {'dev-results':provenance})
    assert all(t['method'] != removed_method for group in provenance['groups'].values() for t in group['frozen'])
    assert any('screened' in group for group in provenance['groups'].values())
    control.run(target, target_config); survive.run(target, target_config); cover.run(target, target_config)
    for name in imported_groups(target, target_config, 'survive'):
        assert frozen_after_control(target, target_config)[name] == frozen_after_control(source, c)[name]
    assert (source/'results/frozen-denominators.json').read_bytes() == original


def test_legacy_child_can_keep_a_method_excluded_only_in_its_parent(tmp_path, monkeypatch):
    c = fixture(tmp_path); c['survival'].update(execution='legacy', control_failures='exclude')
    jobs=[]; setup_worker(monkeypatch, jobs); install_legacy(monkeypatch, jobs)
    run = JavaMavenAdapter.run_group
    def differing_control(self, index, group, rows, phase='A', **kwargs):
        raw = run(self, index, group, rows, phase, **kwargs)
        if phase == 'control' and group == 'dev':
            for t in raw['methods']: t['state'] = 'fail'
        return raw
    monkeypatch.setattr(JavaMavenAdapter, 'run_group', differing_control)
    control.run(tmp_path, c); jobs.clear()
    survive.run(tmp_path, c); cover.run(tmp_path, c)
    assert any(j['group']=='child' and not j.get('coverage') for j in jobs)
    assert all(r['dev_alive']=='0' and r['child_alive']=='1' for r in read_csv(tmp_path/'results/survival.csv'))


def test_changing_admission_replays_control_even_with_identical_stage_hash(tmp_path, monkeypatch):
    from test_survival import pipeline
    c = fixture(tmp_path); c['survival']['execution']='legacy'
    frozen = read_json(tmp_path/'results/frozen-denominators.json')
    records = {g:dict(requested=len(ts), passed=len(ts)-int(g=='dev'),
                       failures=[dict(ts[0], state='fail')] if g=='dev' else []) for g,ts in frozen.items()}
    write_json(tmp_path/'results/control-at-t.json', records)
    write_json(tmp_path/'state/control.json', dict(status='completed', config_hash=stage_hash(c,'control')))
    c['survival']['control_failures']='exclude'
    monkeypatch.setattr(pipeline, 'default_handler', lambda stage:lambda exp,config:finish_control(exp,config,records))
    assert pipeline._run(tmp_path, c, selected=['control']) == ['control']
    assert read_json(tmp_path/'results/control-excluded.json')['dev']
    c['survival']['control_failures']='stop'
    with pytest.raises(RuntimeError, match='Frozen methods failed'):
        pipeline._run(tmp_path, c, selected=['control'])
