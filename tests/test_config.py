from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from test_survival.config import ConfigError, STAGES, SCHEMA, Field, as_written, load, stage_hash, validate


ORIGINAL_HASHES = {
    'prepare': '8a421c9dfd509a39652f2b3e97ff611d6e9cdc9d7aaf3be8dd235f04cfc3fc23',
    'scope': '5a6e85aa10441a021037dd57b190d8d7fcbd415c5cdf16f69540744fb94ca292',
    'generate': '6ff923ca532074d3f0081a728ef4be47701eb63e765ebfcd1936638e071a27ec',
    'screen': '022e1258fe4b243e112636917b7bc50f7f904371d92db540b4db12f15ca1dbb2',
    'control': '022e1258fe4b243e112636917b7bc50f7f904371d92db540b4db12f15ca1dbb2',
    'survive': '054652826351f0b40d1f5eee8b9c22ec3461a51332793a05e811bc53301d3a46',
    'cover': '2d3d66f12bdc51bdcb4a10053e0e2bc8dffc8ea62b35f771f36ebec5d899485e',
    'report': 'a511f93d8b290b9815bb02e307b7e5ac56a9881196d059734154a571a34a24e5',
}

EXAMPLE_HASHES = {
    'prepare': '492cf38a8fe6e8f9d70c0d0870fbc21f53b44a8c766c386fa37a2c8bb4e2549a',
    'scope': '45df1b5d4d5356bdb59a7de00ea19ae4c10ff035947e0208be24cffb8147ef12',
    'generate': '6a01e8175530c2c62c541192240fbd77235f8861ec201e95a95e358ba58c7043',
    'screen': 'c825f2c1fe36ac39fdb6c1c09a18054cb505a124fd3768182726a735c8f9daf2',
    'control': 'c825f2c1fe36ac39fdb6c1c09a18054cb505a124fd3768182726a735c8f9daf2',
    'survive': '75ccd426be2ccab7779645ad04bea741357d29a0c3b39be33e3234ef8d6f2938',
    'cover': 'ece627d971ff22448a66b00cef889b95c81c0fd200c53355cb435c91e7e15d9d',
    'report': 'e16cafe95e6f359e0a23f2f651aec964b52b41f7d2b4271c4a85a9e79abbcefc',
}


@pytest.fixture
def minimal():
    return {'name': 'sample', 'project': {'repo': '/project'}, 'anchor': 'v1'}


def test_defaults_and_acceptance_example(minimal):
    c = validate(minimal)
    assert c['dev']['groups']==[{'name':'dev','display':'Dev','denominator':'project','derive_from':None}]
    assert c['dev']['stable_runs']==3
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


@pytest.mark.parametrize('denominator,start', [('project',5),('coverage-scoped',1)])
def test_environment_settings_invalidate_scope_only_for_coverage_denominators(minimal,denominator,start):
    minimal['dev']={'groups':[{'name':'dev','denominator':denominator}]}
    c=validate(minimal)
    modified=deepcopy(c)
    modified['survival']['environment_errors']['patterns']=['java.net.BindException']
    assert [stage_hash(c,s)!=stage_hash(modified,s) for s in STAGES]==[False]*start+[True]*(8-start)


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



def test_original_written_hashes_are_still_pinned_without_accepting_removed_keys():
    raw=yaml.safe_load((Path(__file__).parent/'fixtures/jsv-run2-original.yaml').read_text())
    assert {stage:stage_hash(raw,stage) for stage in STAGES}==ORIGINAL_HASHES
    with pytest.raises(ConfigError,match='dev_cache|retry_on_zero_compile'):
        validate(raw)


@pytest.mark.parametrize('patch,key', [
    ({'dev':{'groups':[]}},'dev.groups'),
    ({'dev':{'groups':[{'name':'a'},{'name':'a'}]}},'name'),
    ({'dev':{'groups':[{'name':'a','derive_from':'missing'}]}},'derive_from'),
    ({'dev':{'groups':[{'name':'a','derive_from':'b'},{'name':'b','derive_from':'a'}]}},'cycle'),
    ({'dev':{'groups':[{'name':'a'}],'denominator':'project'}},'dev'),
    ({'dev':{'groups':[{'name':'k'}]},'generators':[{'name':'k','tool_path':'/k'}]},'name'),
    ({'survival':{'dev_cache':'none'}},'dev_cache'),
    ({'generators':[{'name':'k','tool_path':'/k','rounds':{'retry_on_zero_compile':0}}]},'retry_on_zero_compile'),
    ({'project':{'repo':'/p','test_framework':'testng'}},'test_framework'),
    ({'range':{'skip_untouched':'test-only'}},'skip_untouched'),
    ({'survival':{'environment_errors':{'patterns':['']}}},'patterns'),
    ({'survival':{'environment_errors':{'max_reruns':-1}}},'max_reruns'),
    ({'survival':{'environment_errors':{'backoff_seconds':-1}}},'backoff_seconds'),
    ({'generators':[{'name':'k','tool_path':'/k','docker':{'image':'x','compose_files':['y']}}]},'docker'),
])
def test_revised_schema_validation(minimal,patch,key):
    minimal.update(patch)
    with pytest.raises(ConfigError,match=key):validate(minimal)


def test_new_defaults_and_generator_override_do_not_mutate_runner(minimal):
    from test_survival.config import generator_config
    minimal['runner']={'docker':{'image':'runner:25'}}
    minimal['generators']=[{'name':'k','tool_path':'/k','docker':{'compose_files':['/k/compose.yml']}}]
    config=validate(minimal)
    assert config['project']['test_framework']=='auto'
    assert config['range']['skip_untouched']=='none'
    assert config['survival']['environment_errors']=={'patterns':[],'max_reruns':3,'backoff_seconds':5}
    assert config['generators'][0]['rounds']['stop_after_zero_compile']==2
    effective=generator_config(config,config['generators'][0])
    assert effective['runner']['docker']['compose_files']==['/k/compose.yml']
    assert effective['runner']['docker']['image'] is None
    assert config['runner']['docker']['image']=='runner:25'
    assert as_written(config)==minimal


def test_optional_env_file_and_maven_volume_defaults_preserve_written_hashes(minimal):
    config = validate(minimal)
    assert config['model']['env_file'] is None
    assert config['runner']['docker']['m2_volume'] == 'survival-m2'
    assert as_written(config) == minimal
    assert all(stage_hash(config, stage) == stage_hash(minimal, stage) for stage in STAGES)


def test_home_paths_expand_without_changing_written_hashes(tmp_path, monkeypatch):
    import os.path
    raw = {'name': 'portable', 'anchor': 't', 'project': {'repo': '~/project'},
           'model': {'env_file': '~/private/model.env'},
           'runner': {'docker': {'compose_files': ['~/compose.yml'], 'env_file': '~/docker.env',
                                 'mounts': {'~/project': '/repo'}},
                      'reuse': {'checkouts': '~/checkouts', 'classpath_cache': '~/classpaths'}},
           'generators': [{'name': 'k', 'tool_path': '~/KTester',
                           'docker': {'compose_files': ['~/KTester/compose.yml'],
                                      'mounts': {'~/KTester': '/work'}, 'env_file': '~/tool.env'}}]}
    original_expand = os.path.expanduser
    for user_dir in (tmp_path/'first-user', tmp_path/'second-user'):
        user_dir.mkdir()
        (user_dir/'experiment.yaml').write_text(yaml.safe_dump(raw))
        monkeypatch.setattr(os.path, 'expanduser', lambda path: str(user_dir) if path == '~' else str(user_dir/path[2:]) if path.startswith('~/') else original_expand(path))
        config = load('~/experiment.yaml')
        assert config['project']['repo'] == str(user_dir/'project')
        assert config['model']['env_file'] == str(user_dir/'private/model.env')
        assert config['runner']['docker']['compose_files'] == [str(user_dir/'compose.yml')]
        assert config['runner']['docker']['mounts'] == {str(user_dir/'project'): '/repo'}
        assert config['runner']['docker']['env_file'] == str(user_dir/'docker.env')
        assert config['runner']['reuse'] == {'checkouts': str(user_dir/'checkouts'), 'classpath_cache': str(user_dir/'classpaths')}
        assert config['generators'][0]['tool_path'] == str(user_dir/'KTester')
        assert config['generators'][0]['docker']['mounts'] == {str(user_dir/'KTester'): '/work'}
        assert config['generators'][0]['docker']['compose_files'] == [str(user_dir/'KTester/compose.yml')]
        assert config['generators'][0]['docker']['env_file'] == str(user_dir/'tool.env')
        assert as_written(config) == as_written(deepcopy(config)) == raw
        assert yaml.safe_load(yaml.safe_dump(config)) == raw
        assert all(stage_hash(config, s) == stage_hash(raw, s) for s in STAGES)
        clone = deepcopy(config)
        clone['runner']['docker']['mounts'][str(user_dir/'project')] = '/changed'
        assert as_written(clone)['runner']['docker']['mounts'] == {'~/project': '/changed'}
        clone['runner']['docker']['compose_files'].append('/another.yml')
        assert as_written(clone)['runner']['docker']['compose_files'] == ['~/compose.yml', '/another.yml']
        clone['runner']['docker']['compose_files'].reverse()
        assert as_written(clone)['runner']['docker']['compose_files'] == ['/another.yml', '~/compose.yml']
        clone['runner']['docker']['mounts']['~/project'] = '/other'
        assert clone['runner']['docker']['mounts'] == {str(user_dir/'project'): '/other'}
        assert as_written(clone)['runner']['docker']['mounts'] == {'~/project': '/other'}
        clone['runner']['docker']['mounts']['~/new'] = '/new'
        assert clone['runner']['docker']['mounts'][str(user_dir/'new')] == '/new'
        assert as_written(clone)['runner']['docker']['mounts']['~/new'] == '/new'
        mounts = deepcopy(clone['runner']['docker']['mounts'])
        assert mounts.setdefault('~/new', '/ignored') == '/new'
        assert mounts.get('~/new') == '/new'
        assert '~/new' in mounts
        assert mounts.pop('~/new') == '/new'
        del mounts['~/project']
        assert as_written(mounts) == {}


@pytest.mark.parametrize('volume', ['/tmp/m2', 'volume:/root/.m2', '../m2', 'bad name', 'm', ''])
def test_maven_volume_requires_a_name(minimal, volume):
    minimal['runner'] = {'docker': {'m2_volume': volume}}
    with pytest.raises(ConfigError, match='m2_volume'):
        validate(minimal)


@pytest.mark.parametrize('patch,field', [
    ({'project': {'repo': '~unresolvable/repo'}}, 'project.repo'),
    ({'model': {'env_file': '~unresolvable/key.env'}}, 'model.env_file'),
    ({'runner': {'reuse': {'checkouts': '~unresolvable/checkouts'}}}, 'runner.reuse.checkouts'),
    ({'runner': {'docker': {'compose_files': ['~unresolvable/compose.yml']}}}, 'runner.docker.compose_files'),
    ({'runner': {'docker': {'mounts': {'~unresolvable/project': '/repo'}}}}, 'runner.docker.mounts'),
    ({'generators': [{'name': 'k', 'tool_path': '~unresolvable/KTester'}]}, 'generators[0].tool_path'),
])
def test_unresolvable_home_reports_configuration_field(minimal, monkeypatch, patch, field):
    def unavailable(path):
        raise RuntimeError('Could not determine home directory.')
    monkeypatch.setattr(Path, 'expanduser', unavailable)
    minimal.update(patch)
    with pytest.raises(ConfigError) as error:
        validate(minimal)
    assert str(error.value).startswith(field+': cannot expand home directory')


def test_unresolvable_experiment_path_is_a_config_error(monkeypatch):
    def unavailable(path):
        raise RuntimeError('Could not determine home directory.')
    monkeypatch.setattr(Path, 'expanduser', unavailable)
    with pytest.raises(ConfigError, match='experiment path: cannot expand home directory'):
        load('~unresolvable/experiment.yaml')
