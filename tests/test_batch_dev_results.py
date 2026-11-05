from copy import deepcopy

import pytest
import yaml

from test_batch_baseline import fixture, setup_worker
from test_survival.dev_results import import_results, imported_groups, imported_baseline
from test_survival.io import read_json, write_csv, write_json
from test_survival.pipeline import PipelineError
from test_survival.rules.dev_denominator import denominator_file
from test_survival.stages import batch, control, survive, cover, scope


@pytest.fixture
def experiments(tmp_path, monkeypatch):
    source, target = tmp_path/'source', tmp_path/'target'
    configs = {}
    setup_worker(monkeypatch, [])
    for exp in (source, target):
        config = fixture(exp)
        config['project']['repo'] = str(tmp_path/'repo')
        config['dev']['groups'] = config['dev']['groups'][:1]
        config['generators'] = []
        frozen = read_json(exp/'results/frozen-denominators.json')
        write_json(exp/'results/frozen-denominators.json', {'dev':frozen['dev']})
        write_csv(exp/denominator_file(config['dev']['groups'][0], 0), frozen['dev'], list(frozen['dev'][0]))
        write_csv(exp/'dev/dev-methods-all.csv', [dict(t, stable='yes') for t in frozen['dev']], [*frozen['dev'][0], 'stable'])
        (exp/'experiment.yaml').write_text(yaml.safe_dump(config))
        configs[exp] = config
    control.run(source, configs[source]); survive.run(source, configs[source]); cover.run(source, configs[source])
    return source, target, configs[target]


@pytest.mark.parametrize('field', ['class', 'file'])
def test_batch_import_refuses_different_resolved_scope_before_copying(experiments, field):
    source, target, config = experiments
    resolved = read_json(target/'population/scope.json')
    resolved['classes'][0][field] = 'B' if field == 'class' else 'src/main/java/B.java'
    write_json(target/'population/scope.json', resolved)
    before = {p:p.read_bytes() for exp in (source, target) for p in exp.rglob('*') if p.is_file()}
    with pytest.raises(PipelineError, match='resolved coverage scope'):
        import_results(target, source, config)
    assert before == {p:p.read_bytes() for exp in (source, target) for p in exp.rglob('*') if p.is_file()}


@pytest.mark.parametrize('side', ['source', 'target'])
def test_batch_import_requires_resolved_scope(experiments, side):
    source, target, config = experiments
    ((source if side == 'source' else target)/'population/scope.json').unlink()
    with pytest.raises(PipelineError, match='resolved coverage scope.*scope'):
        import_results(target, source, config)


def test_batch_import_normalizes_scope_and_rechecks_consumers(experiments, monkeypatch):
    source, target, config = experiments
    resolved = read_json(target/'population/scope.json')
    resolved['classes'][0].update(m=99, c=7, d=2)
    resolved['classes'] *= 2
    write_json(target/'population/scope.json', resolved)
    provenance = import_results(target, source, config)
    assert provenance['compatibility']['resolved_scope'] == [{'class':'A', 'file':'src/main/java/A.java'}]
    write_json(target/'state/import.json', {'dev-results':provenance})
    monkeypatch.setattr(batch, 'measurement_session', lambda *args: pytest.fail('incompatible import launched Java'))
    resolved['classes'][0]['file'] = 'src/main/java/B.java'
    write_json(target/'population/scope.json', resolved)
    for consumer in (control.run, survive.run, cover.run, imported_baseline):
        with pytest.raises(PipelineError, match='resolved coverage scope'):
            consumer(target, config)
    from test_survival.progress import items, snapshot
    assert items(target, 'survive', config, {})[0] == []
    assert snapshot(target, 'survive', config)['done'] == 0


def test_old_batch_import_manifest_without_scope_proof_is_refused(experiments):
    source, target, config = experiments
    provenance = import_results(target, source, config)
    provenance['compatibility'].pop('resolved_scope', None)
    write_json(target/'state/import.json', {'dev-results':provenance})
    with pytest.raises(PipelineError, match='resolved coverage scope'):
        imported_groups(target, config)


def test_scope_reselection_checks_import_before_using_denominators(experiments, monkeypatch):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.runner.docker import DockerRunner
    source, target, config = experiments
    provenance = import_results(target, source, config)
    write_json(target/'state/import.json', {'dev-results':provenance})
    write_json(target/'results/source-inventory.json', {'main':{'methods':[], 'classes':{}}, 'test':{'classes':{}}})
    monkeypatch.setattr(JavaMavenAdapter, 'ensure_anchor', lambda *args: None)
    def audit(self, command, cwd, log, timeout):
        log.write_text('class\tmethod\taccessor\n')
        return {'exit_code':0}
    monkeypatch.setattr(DockerRunner, 'execute', audit)
    changed = deepcopy(read_json(target/'population/scope.json')['classes'])
    changed[0]['class'] = 'B'
    monkeypatch.setattr(scope, 'select_scope', lambda *_: (changed, 1))
    before = (target/'dev/dev-denominator.csv').read_bytes()
    with pytest.raises(PipelineError, match='resolved coverage scope'):
        scope.run(target, config)
    assert (target/'dev/dev-denominator.csv').read_bytes() == before
