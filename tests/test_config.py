from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from test_survival.config import ConfigError, STAGES, SCHEMA, Field, as_written, load, stage_hash, validate


EXAMPLE_HASHES = {
    'prepare': '0ef980fde3cd48e474b0a262d153b1c26bda0a9b3ec6f699ce1364b407fc581b',
    'scope': '9c69904ee4ac24e42dd455e6475f8068e01ada3378dc9e6111756445e9b04623',
    'generate': '36b9b01922e96970acee7112f46f5eb42417158b4a3e0e0f28ca4942ffea6f26',
    'screen': '89b92bcb91f680a06bb4608e1435346f1fbfbc86722bba8f4f721bc722847a45',
    'control': '89b92bcb91f680a06bb4608e1435346f1fbfbc86722bba8f4f721bc722847a45',
    'survive': '42407e16987d4b965028d1e17ced58cf8761262cde61d0e16b5d86ce36583ed0',
    'cover': '5abda39490deb68908123a5e406c9c192bc76bbc420a8b82065d0374115a33e7',
    'report': '2d76c1cf13e442a3c04b9a49886b308dc44db78477c73a88a0166a41a8965cf1',
}


@pytest.fixture
def minimal():
    return {'name': 'sample', 'project': {'repo': '/project'}, 'anchor': 'v1'}


def test_defaults_and_acceptance_example(minimal):
    c = validate(minimal)
    assert c['dev'] == {'denominator': 'project', 'stable_runs': 3}
    assert c['runner']['workers'] == {'build': 4, 'survive': 4, 'cover': 4}
    assert c['scope']['rank_by'] == 'change-count'
    assert len(load(Path(__file__).parents[1] / 'examples/jsv-run2/experiment.yaml')['generators']) == 4
    assert minimal == {'name': 'sample', 'project': {'repo': '/project'}, 'anchor': 'v1'}


@pytest.mark.parametrize('patch, key', [
    ({'unexpected': 3}, 'unexpected'),
    ({'runner': {'workers': {'survive': False}}}, 'runner.workers.survive'),
    ({'scope': {'target_methods': [80, 50]}}, 'scope.target_methods'),
    ({'scope': {'rule': 'explicit'}}, 'scope.classes'),
    ({'scope': {'rank_by': 'random'}}, 'scope.rank_by'),
    ({'screen': {'matching': 'random'}}, 'screen.matching'),
    ({'project': {'repo': '/p', 'language': 'javascript', 'build': 'npm'}}, 'not implemented in this version'),
    ({'project': {'repo': '/p', 'module': '../escape'}}, 'project.module'),
    ({'generators': [{'name': 'dev', 'tool_path': '/k'}]}, 'name'),
    ({'generators': [{'name': 'k', 'tool_path': '/k', 'params': {'key': 'secret'}}]}, 'params.key'),
    ({'generators': [{'name': 'o', 'method': 'ouedraogo', 'impl': 'native', 'params': {'prompts': ['bogus']}}]}, 'params.prompts'),
    ({'runner': {'docker': {'image': 'x', 'compose_files': ['/x']}}}, 'alternatives'),
    ({'range': {'limit': -1}}, 'range.limit'),
    ({'runner': {'jvm': {'javac': '0g'}}}, 'runner.jvm.javac'),
])
def test_invalid(minimal, patch, key):
    minimal.update(patch)
    with pytest.raises(ConfigError, match=key):
        validate(minimal)


def test_hash_boundaries_and_key_order(minimal):
    c = validate(minimal)
    modified = deepcopy(c)
    modified['screen']['stable_runs'] = 4
    assert [stage_hash(c, s) != stage_hash(modified, s) for s in STAGES] == [False]*3+[True]*5
    modified = deepcopy(c)
    modified['coverage']['at_anchor'] = False
    assert [stage_hash(c, s) != stage_hash(modified, s) for s in STAGES] == [False]*6+[True]*2
    assert stage_hash(c, 'report') == stage_hash(dict(reversed(list(as_written(c).items()))), 'report')


def test_duplicate_yaml_and_secret_not_echoed(tmp_path):
    path = tmp_path / 'experiment.yaml'
    path.write_text('name: a\nname: b\n')
    with pytest.raises(ConfigError, match='duplicate'):
        load(path)
    path.write_text('name: [DO_NOT_ECHO_SECRET\n')
    with pytest.raises(ConfigError) as exc:
        load(path)
    assert 'DO_NOT_ECHO_SECRET' not in str(exc.value)


def test_template_is_complete_valid_yaml():
    path = Path(__file__).parents[1] / 'src/test_survival/experiment-template.yaml'
    validate(yaml.safe_load(path.read_text()))


def test_acceptance_example_stage_hashes_are_pinned():
    path = Path(__file__).parents[1] / 'examples/jsv-run2/experiment.yaml'
    config = load(path)
    assert as_written(config) == yaml.safe_load(path.read_text())
    assert {stage: stage_hash(config, stage) for stage in STAGES} == EXAMPLE_HASHES


def test_new_optional_defaults_do_not_change_existing_hashes(monkeypatch):
    monkeypatch.setitem(SCHEMA['scope'], 'future_optional', Field(str, 'new-default'))
    config = load(Path(__file__).parents[1] / 'examples/jsv-run2/experiment.yaml')
    assert config['scope']['future_optional'] == 'new-default'
    assert {stage: stage_hash(config, stage) for stage in STAGES} == EXAMPLE_HASHES


@pytest.mark.parametrize('rank_by', ['change-count', 'method-count'])
def test_explicit_rank_by_affects_scope_and_later_hashes(rank_by):
    config = load(Path(__file__).parents[1] / 'examples/jsv-run2/experiment.yaml')
    config['scope']['rank_by'] = rank_by
    assert [stage_hash(config, stage) != EXAMPLE_HASHES[stage] for stage in STAGES] == [False] + [True]*7


def test_yaml_roundtrip_keeps_omitted_defaults_omitted(minimal):
    config = validate(minimal)
    assert yaml.safe_load(yaml.safe_dump(config)) == minimal
    clone = deepcopy(config)
    clone['scope'].update(rank_by='change-count')
    assert as_written(clone)['scope'] == {'rank_by': 'change-count'}
    assert as_written(config) == minimal


def test_explicit_null_keeps_written_value_when_execution_fills_display(minimal):
    minimal['generators'] = [{'name': 'k', 'tool_path': '/k', 'display': None}]
    config = validate(minimal)
    assert config['generators'][0]['display'] == 'k'
    assert as_written(config) == minimal
    config['generators'][0]['display'] = 'k'
    assert as_written(config)['generators'][0]['display'] == 'k'
