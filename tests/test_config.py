from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from test_survival.config import ConfigError, STAGES, load, stage_hash, validate


@pytest.fixture
def minimal():
    return {'name': 'sample', 'project': {'repo': '/project'}, 'anchor': 'v1'}


def test_defaults_and_acceptance_example(minimal):
    c = validate(minimal)
    assert c['dev'] == {'denominator': 'project', 'stable_runs': 3}
    assert c['runner']['workers'] == {'build': 4, 'survive': 4, 'cover': 4}
    assert len(load(Path(__file__).parents[1] / 'examples/jsv-run2/experiment.yaml')['generators']) == 4
    assert minimal == {'name': 'sample', 'project': {'repo': '/project'}, 'anchor': 'v1'}


@pytest.mark.parametrize('patch, key', [
    ({'unexpected': 3}, 'unexpected'),
    ({'runner': {'workers': {'survive': False}}}, 'runner.workers.survive'),
    ({'scope': {'target_methods': [80, 50]}}, 'scope.target_methods'),
    ({'scope': {'rule': 'explicit'}}, 'scope.classes'),
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
    assert stage_hash(c, 'report') == stage_hash(dict(reversed(list(c.items()))), 'report')


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
