from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from test_survival.config import ConfigError, STAGES, SCHEMA, Field, as_written, load, stage_hash, validate


ORIGINAL_HASHES = {
    'prepare': '411a76334ae02c686e52600ecd8856da787d3a9e8ca2ef13fe9755ed7570b939',
    'scope': '644ba28297719d17cb21e438806ccea505b834b3b2a0285b927cc1a10aa4d640',
    'generate': 'cf952eb408c7ffdc73b97dd8c77881d23e727572f8c1826e3a0d05ecc9df6a94',
    'screen': 'ef2deb283b9e1470c9f9372d1495beb312309d5a72926b90f77cca68d6e80bca',
    'control': 'ef2deb283b9e1470c9f9372d1495beb312309d5a72926b90f77cca68d6e80bca',
    'survive': '5e1433d9f12119ec9acad05b14c94fc254fc3b36feaae091355177e4df159abf',
    'cover': '9ad08fe086dc3fbecfaa0dc06ed6576cd523d8641d2aca26b7bb54437a192707',
    'report': '159239ebe417b7decd25d8446bb7a508d84e0563858f727f3a7881137f30f525',
}

EXAMPLE_HASHES = {
    'prepare': '1fa9dcdad95d323aa66e31cdc4c1b41fa1886dec4d54be3e306ddf006e16e89d',
    'scope': 'f084348b8bbabf9c2ace514d6e8d1b926fb133d156f0d91f82741fe29bdb92c1',
    'generate': '0e735d4ec7f02ea6b9ed6848146ccc0a3174478be01162c2c521fcdf0e3c3302',
    'screen': '0a23e6358db39d24c15228d38d282b7c78d8b193ed0fab07266be17280f30cd6',
    'control': '0a23e6358db39d24c15228d38d282b7c78d8b193ed0fab07266be17280f30cd6',
    'survive': '54a02d916496c770e559ccf03c09807d1ee444888a949d1fdb0332169d581627',
    'cover': '7609c0c68d22a41bd0deb25ea355c312602fc84388dd960dc055a539eb3d24d6',
    'report': '89bde1a9ea1821852a0516a15d9f1390042945c5a9a63a252638ca578c3ccebf',
}


@pytest.fixture
def minimal():
    return {'name': 'sample', 'project': {'repo': '/project'}, 'anchor': 'v1'}


def test_defaults_and_acceptance_example(minimal):
    c = validate(minimal)
    assert c['dev']['groups']==[{'name':'dev','display':'Dev','denominator':'project','derive_from':None}]
    assert c['dev']['stable_runs']==3
    assert c['dev']['unmapped']=='error'
    assert c['runner']['workers'] == {'build': 4, 'survive': 4, 'cover': 4}
    assert c['scope']['rank_by'] == 'change-count'
    assert len(load(Path(__file__).parents[1] / 'examples/jsv-run2/experiment.yaml')['generators']) == 4
    assert minimal == {'name': 'sample', 'project': {'repo': '/project'}, 'anchor': 'v1'}


@pytest.mark.parametrize('patch, key', [
    ({'unexpected': 3}, 'unexpected'),
    ({'dev': {'unmapped': 'ignore'}}, 'dev.unmapped'),
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


def test_unmapped_policy_invalidates_prepare_and_later_stages(minimal):
    config = validate(minimal)
    changed = validate({**minimal, 'dev': {'unmapped': 'exclude'}})
    assert changed['dev']['unmapped'] == 'exclude'
    assert all(stage_hash(config, stage) != stage_hash(changed, stage) for stage in STAGES)


@pytest.mark.parametrize('name', ['dev-unmapped', 'Dev-Unmapped'])
@pytest.mark.parametrize('policy', ['error', 'exclude'])
def test_developer_group_cannot_overwrite_shared_unmapped_audit(minimal, name, policy):
    minimal['dev'] = {'groups': [{'name': name}], 'unmapped': policy}
    with pytest.raises(ConfigError, match=r'dev.groups\[0\].name:.*reserved.*audit'):
        validate(minimal)


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


@pytest.mark.parametrize('keys,value', [
    (('runner', 'workers', 'build'), 9),
    (('runner', 'workers', 'survive'), 2),
    (('runner', 'workers', 'cover'), 6),
    (('runner', 'reuse', 'checkouts'), '/other/checkouts'),
    (('runner', 'reuse', 'classpath_cache'), '/other/classpath'),
    (('runner', 'docker', 'mounts'), {'/other/repo': '/repo'}),
    (('runner', 'docker', 'env_file'), '/private/other.env'),
    (('model', 'env_file'), '/private/model.env'),
])
def test_operational_settings_do_not_change_any_stage_hash(minimal, keys, value):
    config = validate(minimal)
    before = as_written(config)
    changed = deepcopy(config)
    parent = changed
    for key in keys[:-1]:
        parent = parent[key]
    parent[keys[-1]] = value
    assert all(stage_hash(config, stage) == stage_hash(changed, stage) for stage in STAGES)
    assert as_written(config) == before
    assert parent[keys[-1]] == value  # Filtering must not mutate execution settings.


@pytest.mark.parametrize('keys,value', [
    (('docker', 'image'), 'jdk:25'),
    (('docker', 'compose_files'), ['/runner/compose.yml']),
    (('timeouts', 'build'), 900),
    (('timeouts', 'compile'), 600),
    (('timeouts', 'test_class'), 200),
    (('jvm', 'launcher'), '2g'),
    (('jvm', 'javac'), '1g'),
    (('jvm', 'jacoco'), '2g'),
])
def test_result_affecting_runner_settings_still_change_hashes(minimal, keys, value):
    config = validate(minimal)
    changed = deepcopy(config)
    changed['runner'][keys[0]][keys[1]] = value
    assert all(stage_hash(config, stage) != stage_hash(changed, stage) for stage in STAGES)


def test_generator_docker_override_ignores_only_host_wiring(minimal):
    minimal['generators'] = [{'name': 'k', 'tool_path': '/k', 'docker': {'image': 'jdk:17'}}]
    config = validate(minimal)
    changed = deepcopy(config)
    changed['generators'][0]['docker'].update(mounts={'/host': '/repo'}, env_file='/host.env')
    assert all(stage_hash(config, stage) == stage_hash(changed, stage) for stage in STAGES)
    assert stage_hash(config, 'generate', 'k') == stage_hash(changed, 'generate', 'k')
    changed['generators'][0]['docker']['image'] = 'jdk:25'
    assert stage_hash(config, 'generate', 'k') != stage_hash(changed, 'generate', 'k')
