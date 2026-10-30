"""Strict YAML schema and cumulative configuration fingerprints."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re

import yaml

STAGES = ('prepare', 'scope', 'generate', 'screen', 'control', 'survive', 'cover', 'report')
MISSING = object()


class ConfigError(ValueError):
    pass


def expand_path(path, field):
    try:
        return Path(path).expanduser()
    except RuntimeError as exc:
        raise ConfigError(f'{field}: cannot expand home directory in {str(path)!r}') from exc


class Configuration(dict):
    """Resolved values for execution, retaining explicitly supplied hash inputs."""

    def __init__(self, resolved, written):
        self._explicit = set(written)
        self._written = deepcopy(written)
        self._initial = deepcopy(resolved)
        self._written_keys = {}
        self._path_values = {}
        self._path_key_prefix = None
        dict.__init__(self, {key: self._wrap(value, written.get(key, MISSING)) for key, value in resolved.items()})

    @staticmethod
    def _wrap(value, written):
        if isinstance(value, Configuration):
            return value
        if isinstance(value, dict):
            return Configuration(value, written if isinstance(written, dict) else {})
        if isinstance(value, list):
            supplied = written if isinstance(written, list) else []
            return [Configuration._wrap(v, supplied[i] if i < len(supplied) else MISSING) for i, v in enumerate(value)]
        return value

    def _key(self, key):
        return str(expand_path(key, self._path_key_prefix)) if self._path_key_prefix else key

    def __getitem__(self, key):
        return dict.__getitem__(self, self._key(key))

    def __contains__(self, key):
        return dict.__contains__(self, self._key(key))

    def get(self, key, default=None):
        return dict.get(self, self._key(key), default)

    def __setitem__(self, key, value):
        if self._path_key_prefix:
            written = key
            key = self._key(key)
            if key != written:
                self._written_keys[key] = written
        self._path_values.pop(key, None)
        self._explicit.add(key)
        self._written[key] = deepcopy(value)
        dict.__setitem__(self, key, self._wrap(value, value))

    def __delitem__(self, key):
        key = self._key(key)
        dict.__delitem__(self, key)
        self._explicit.discard(key)
        self._written_keys.pop(key, None)
        self._path_values.pop(key, None)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key, *default):
        key = self._key(key)
        value = dict.pop(self, key, *default)
        self._explicit.discard(key)
        self._written_keys.pop(key, None)
        self._path_values.pop(key, None)
        return value

    def popitem(self):
        key, value = dict.popitem(self)
        self._explicit.discard(key)
        self._written_keys.pop(key, None)
        self._path_values.pop(key, None)
        return key, value

    def clear(self):
        dict.clear(self)
        self._explicit.clear()
        self._written_keys.clear()
        self._path_values.clear()

    def __ior__(self, other):
        self.update(other)
        return self

    def __deepcopy__(self, memo):
        clone = Configuration({}, {})
        memo[id(self)] = clone
        clone._explicit = self._explicit.copy()
        clone._written = deepcopy(self._written, memo)
        clone._initial = deepcopy(self._initial, memo)
        clone._written_keys = self._written_keys.copy()
        clone._path_values = deepcopy(self._path_values, memo)
        clone._path_key_prefix = self._path_key_prefix
        for key, value in self.items():
            dict.__setitem__(clone, key, deepcopy(value, memo))
        return clone


def as_written(value, *, include_defaults=False):
    if isinstance(value, Configuration):
        result = {}
        for key, item in value.items():
            supplied = as_written(item, include_defaults=include_defaults)
            if isinstance(item, list) and key in value._path_values:
                supplied = [value._path_values[key].get(v, v) for v in item]
            scalar_or_list = not isinstance(item, dict) and (not isinstance(item, list) or all(not isinstance(v, (dict, list)) for v in item))
            if scalar_or_list and key in value._written and item == value._initial.get(key, MISSING):
                supplied = value._written[key]
            if include_defaults or key in value._explicit or (isinstance(item, Configuration) and supplied) or item != value._initial.get(key, MISSING):
                result[value._written_keys.get(key, key)] = supplied
        return result
    if isinstance(value, dict):
        return {key: as_written(item, include_defaults=include_defaults) for key, item in value.items()}
    if isinstance(value, list):
        return [as_written(item, include_defaults=include_defaults) for item in value]
    return value


yaml.SafeDumper.add_representer(Configuration, lambda dumper, value: dumper.represent_dict(as_written(value)))


@dataclass(frozen=True)
class Field:
    kind: object
    default: object = MISSING
    choices: tuple = ()
    minimum: float | None = None
    nullable: bool = False


def enum(default, *choices):
    return Field(str, default, choices)


def integer(default, minimum=1, nullable=False):
    return Field(int, default, minimum=minimum, nullable=nullable)


DOCKER = {
    'compose_files': Field(list, []), 'service': Field(str, 'ktester'),
    'image': Field(str, None, nullable=True), 'mounts': Field(dict, {}),
    'env_file': Field(str, None, nullable=True),
    'm2_volume': Field(str, 'survival-m2'),
}
DEV_GROUP = {
    'name': Field(str), 'display': Field(str, None, nullable=True),
    'denominator': enum('project', 'project', 'scope-targeted', 'coverage-scoped'),
    'derive_from': Field(str, None, nullable=True), 'frozen': Field(bool, True),
}


GENERATOR = {
    'name': Field(str), 'display': Field(str, None, nullable=True),
    'method': enum('ktester', 'ktester', 'ouedraogo', 'testpilot'),
    'impl': enum('author-tool', 'author-tool', 'native'),
    'tool_path': Field(str, None, nullable=True), 'params': Field(dict, {}),
    'docker': Field(dict, None, nullable=True),
    'rounds': {'max': integer(1), 'until_dev_matched': Field(bool, False),
               'stop_after_zero_compile': integer(2, 0)},
    'context': {'dependency_signatures': {'enabled': Field(bool, False), 'max_types': integer(15), 'max_lines': integer(400)}},
    'prompt': {'api_constraint': Field(bool, False)},
    'postprocess': {'import_repair': {'enabled': Field(bool, False), 'passes': integer(2)}},
}
SCHEMA = {
    'policies': Field(dict, None, nullable=True),
    'name': Field(str),
    'project': {'repo': Field(str), 'language': enum('java', 'java', 'javascript'),
                'build': enum('maven', 'maven', 'npm', 'pnpm'), 'module': Field(str, '.'),
                'test_framework': enum('auto', 'auto', 'junit5', 'junit4')},
    'anchor': Field(str),
    'range': {'branch': Field(str, 'origin/master'), 'end': Field(str, None, nullable=True),
              'mode': enum('first-parent', 'first-parent'), 'limit': integer(None, 1, True),
              'skip_untouched': enum('none', 'none', 'module', 'main-sources')},
    'scope': {'rule': enum('change-ranked', 'dedicated-test', 'change-ranked', 'explicit', 'all-eligible'),
              'rank_by': enum('change-count', 'change-count', 'method-count'),
              'min_methods': integer(3), 'exclude_accessor_heavy': Field(bool, True),
              'target_methods': Field(list, [50, 80]), 'max_classes': integer(10),
              'classes': Field(list, []), 'min_classes': integer(1)},
    'dev': {'denominator': enum('project', 'project', 'scope-targeted', 'coverage-scoped'), 'stable_runs': integer(3),
            'groups': Field(list, []), 'unmapped': enum('error', 'error', 'exclude')},
    'model': {'provider': enum('openrouter', 'openrouter'), 'name': Field(str, 'openai/gpt-oss-120b'),
              'env_file': Field(str, None, nullable=True),
              'env_key': Field(str, 'OPENROUTER_API_KEY')},
    'generators': Field(list, []),
    'screen': {'stable_runs': integer(3), 'dedup': enum('exact-body', 'exact-body'),
               'matching': enum('none', 'none', 'subsample-to-dev')},
    'survival': {'definition': enum('cumulative-fixed-denominator', 'cumulative-fixed-denominator'),
                 'flaky_rerun': integer(1, 0), 'build_fail': enum('carry-over', 'carry-over'),
                 'environment_errors': {'patterns': Field(list, []), 'max_reruns': integer(3, 0),
                                        'backoff_seconds': Field(float, 5, minimum=0)}},
    'coverage': {'changed_line': Field(bool, True), 'at_anchor': Field(bool, True),
                 'groups': enum('alive-entering', 'alive-entering')},
    'runner': {
        'docker': DOCKER,
        'javac_mode': enum('subprocess', 'subprocess', 'reused-jvm'),
        'reuse': {'checkouts': Field(str, None, nullable=True), 'classpath_cache': Field(str, None, nullable=True)},
        'workers': {key: integer(4) for key in ('build', 'survive', 'cover')},
        'timeouts': {'build': integer(600), 'compile': integer(300), 'test_class': integer(120)},
        'jvm': {key: Field(str, value) for key, value in [('launcher', '1g'), ('javac', '512m'), ('jacoco', '1g')]},
        'cache': {'classpath': enum('pom-sha256', 'pom-sha256', 'none'),
                  'node_modules': enum('lockfile-sha256', 'lockfile-sha256')},
    },
}
PARAMS = {
    'ktester': {'fix_tries': integer(5, 0), 'temperature': Field(float, 0.5, minimum=0), 'workers': integer(8)},
    'ouedraogo': {'temperature': Field(float, 0.7, minimum=0), 'max_tokens': integer(16384),
                  'prompts': Field(list, ['ZSL', 'FSL', 'CoT', 'ToT', 'GToT'])},
}


def _validate(data, schema, prefix=''):
    if not isinstance(data, dict):
        raise ConfigError(f'{prefix or "config"}: expected a mapping')
    for key in data:
        if key not in schema:
            raise ConfigError(f'{prefix}{key}: unknown key; allowed keys: {", ".join(schema)}')
    result = {}
    for key, spec in schema.items():
        path = prefix + key
        if isinstance(spec, dict):
            result[key] = _validate(data.get(key, {}), spec, path + '.')
            continue
        value = data.get(key, MISSING)
        if value is MISSING:
            if spec.default is MISSING:
                raise ConfigError(f'{path}: required')
            value = deepcopy(spec.default)
        if value is None and spec.nullable:
            result[key] = None
            continue
        valid = type(value) is spec.kind
        if spec.kind is float:
            valid = type(value) in (int, float) and math.isfinite(value)
        if not valid or (spec.kind is str and not value.strip()):
            raise ConfigError(f'{path}: expected nonempty {spec.kind.__name__}')
        if spec.choices and value not in spec.choices:
            raise ConfigError(f'{path}: allowed values: {", ".join(spec.choices)}')
        if spec.minimum is not None and value < spec.minimum:
            raise ConfigError(f'{path}: must be >= {spec.minimum}')
        result[key] = value
    return result


def _strings(values, path):
    if any(type(s) is not str or not s.strip() for s in values):
        raise ConfigError(f'{path}: expected a list of nonempty strings')
    if len(set(values)) != len(values):
        raise ConfigError(f'{path}: duplicate entries are not allowed')


def _docker(data, prefix):
    docker = _validate(data, DOCKER, prefix+'.')
    _strings(docker['compose_files'], prefix+'.compose_files')
    if docker['compose_files'] and docker['image']:
        raise ConfigError(prefix+': compose_files and image are alternatives')
    for host,target in docker['mounts'].items():
        if not isinstance(host,str) or not isinstance(target,str) or not expand_path(host, prefix+'.mounts').is_absolute() or not target.startswith('/'):
            raise ConfigError(prefix+'.mounts: expected absolute host path -> absolute container path')
    if len({str(expand_path(host, prefix+'.mounts')) for host in docker['mounts']}) != len(docker['mounts']):
        raise ConfigError(prefix+'.mounts: duplicate host paths after expanding ~')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]+', docker['m2_volume']):
        raise ConfigError(prefix+'.m2_volume: expected a named Docker volume')
    return docker


def _expand_paths(config):
    """Expand host paths for execution while hashing the YAML as written."""
    def expand(path, prefix):
        return str(expand_path(path, prefix)) if path and path.startswith('~') else path

    def field(node, name, prefix):
        value = node[name]
        resolved = [expand(p, prefix+'.'+name) for p in value] if isinstance(value, list) else expand(value, prefix+'.'+name)
        if isinstance(value, list):
            node._path_values[name] = dict(zip(resolved, value))
        dict.__setitem__(node, name, resolved)
        node._initial[name] = deepcopy(resolved)

    def docker(node, prefix):
        field(node, 'compose_files', prefix)
        field(node, 'env_file', prefix)
        mounts = node['mounts']
        mounts._path_key_prefix = prefix+'.mounts'
        for host in list(mounts):
            resolved = expand(host, prefix+'.mounts')
            if resolved == host:
                continue
            dict.__setitem__(mounts, resolved, dict.pop(mounts, host))
            mounts._initial[resolved] = mounts._initial.pop(host)
            mounts._written[resolved] = mounts._written.pop(host)
            mounts._explicit.discard(host)
            mounts._explicit.add(resolved)
            mounts._written_keys[resolved] = host

    field(config['project'], 'repo', 'project')
    field(config['model'], 'env_file', 'model')
    for name in ('checkouts', 'classpath_cache'):
        field(config['runner']['reuse'], name, 'runner.reuse')
    docker(config['runner']['docker'], 'runner.docker')
    for index, group in enumerate(config['generators']):
        prefix = f'generators[{index}]'
        field(group, 'tool_path', prefix)
        if group['docker'] is not None:
            docker(group['docker'], prefix+'.docker')
    return config


def _group_name(name, names, prefix):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*',name) or name in names:
        raise ConfigError(prefix+'name: must be unique across developer and generator groups and contain only letters, digits, _ or -')
    names.add(name)


def developer_groups(config):
    return config['dev']['groups']


def group_names(config):
    return [g['name'] for g in [*developer_groups(config),*config['generators']] if g.get('frozen', True)]


def group_labels(config):
    return {g['name']:g['display'] for g in [*developer_groups(config),*config['generators']]}


def generator_config(config, generator):
    """A generator's Docker mapping replaces the runner mapping, in a copy."""
    result=deepcopy(config)
    if generator.get('docker') is not None:
        result['runner']['docker']=deepcopy(generator['docker'])
    return result


def validate(data):
    data = as_written(data)
    c = _validate(data, SCHEMA)
    if c['policies'] is not None:
        from .policy.config import validate as validate_policies
        c['policies'] = validate_policies(c['policies'])
    if (c['project']['language'], c['project']['build']) != ('java', 'maven'):
        raise ConfigError('project.language/project.build: not implemented in this version of the tool; implemented: java/maven')
    module = Path(c['project']['module'])
    if module.is_absolute() or '..' in module.parts:
        raise ConfigError('project.module: expected a relative path without ..')
    targets = c['scope']['target_methods']
    if len(targets) != 2 or any(type(n) is not int or n < 1 for n in targets) or targets[0] > targets[1]:
        raise ConfigError('scope.target_methods: expected [positive minimum, maximum >= minimum]')
    if c['scope']['rule'] != 'all-eligible' and c['scope']['min_classes'] > c['scope']['max_classes']:
        raise ConfigError('scope.min_classes: must be <= scope.max_classes')
    _strings(c['scope']['classes'], 'scope.classes')
    if c['scope']['rule'] == 'explicit' and not c['scope']['classes']:
        raise ConfigError('scope.classes: required nonempty list for explicit scope')
    raw_dev=data.get('dev',{})
    if 'groups' in raw_dev and 'denominator' in raw_dev:
        raise ConfigError('dev: use groups or denominator shorthand, not both')
    if 'groups' in raw_dev and not c['dev']['groups']:
        raise ConfigError('dev.groups: expected at least one group')
    groups=c['dev']['groups'] if 'groups' in raw_dev else [{'name':'dev','denominator':c['dev']['denominator']}]
    names=set();resolved=[]
    for i,raw in enumerate(groups):
        prefix=f'dev.groups[{i}].';g=_validate(raw,DEV_GROUP,prefix)
        _group_name(g['name'],names,prefix)
        # Scope writes dev/{name}-excluded.csv, also on case-insensitive filesystems.
        if g['name'].lower() == 'dev-unmapped':
            raise ConfigError(prefix+'name: dev-unmapped is reserved for the shared baseline exclusion audit; choose another name')
        g['display']=g['display'] or ('Dev' if g['name']=='dev' else g['name'])
        resolved.append(g)
    links={g['name']:g['derive_from'] for g in resolved}
    for i,g in enumerate(resolved):
        seen={g['name']};source=g['derive_from']
        while source is not None:
            if source not in links:raise ConfigError(f'dev.groups[{i}].derive_from: expected a developer group name')
            if source in seen:raise ConfigError(f'dev.groups[{i}].derive_from: dependency cycle')
            seen.add(source);source=links[source]
    c['dev']['groups']=resolved
    _strings(c['survival']['environment_errors']['patterns'],'survival.environment_errors.patterns')
    generators = []
    for i, raw in enumerate(c['generators']):
        prefix = f'generators[{i}].'
        g = _validate(raw, GENERATOR, prefix)
        _group_name(g['name'],names,prefix)
        if g['docker'] is not None:g['docker']=_docker(g['docker'],prefix+'docker')
        if (g['method'], g['impl']) not in (('ktester', 'author-tool'), ('ouedraogo', 'native')):
            raise ConfigError(prefix + 'method/impl: not implemented in this version of the tool; implemented: ktester/author-tool, ouedraogo/native')
        if g['impl'] == 'author-tool' and g['tool_path'] is None:
            raise ConfigError(prefix + 'tool_path: required for author-tool')
        g['display'] = g['display'] or g['name']
        g['params'] = _validate(g['params'], PARAMS[g['method']], prefix + 'params.')
        if g['params']['temperature'] > 2:
            raise ConfigError(prefix + 'params.temperature: must be <= 2')
        if g['method'] == 'ouedraogo':
            prompts = g['params']['prompts']
            _strings(prompts, prefix + 'params.prompts')
            if not prompts or set(prompts) - {'ZSL', 'FSL', 'CoT', 'ToT', 'GToT'}:
                raise ConfigError(prefix + 'params.prompts: allowed values: ZSL, FSL, CoT, ToT, GToT; list must be nonempty')
        generators.append(g)
    c['generators'] = generators
    c['runner']['docker']=_docker(c['runner']['docker'],'runner.docker')
    for key, value in c['runner']['jvm'].items():
        if not re.fullmatch(r'[1-9][0-9]*[kKmMgG]?', value):
            raise ConfigError(f'runner.jvm.{key}: expected a positive JVM memory size, such as 512m or 1g')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', c['model']['env_key']):
        raise ConfigError('model.env_key: expected an environment variable name')
    return _expand_paths(Configuration(c, data))


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently accepting the last one."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in result:
                raise ConfigError('YAML: duplicate mapping key')
            result[key] = loader.construct_object(value_node, deep=deep)
        except TypeError as exc:
            raise ConfigError('YAML: mapping keys must be scalar') from exc
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load(path):
    path = expand_path(path, 'experiment path')
    if path.is_dir():
        path = path / 'experiment.yaml'
    try:
        data = yaml.load(path.read_text(), Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        # Do not echo YAML lines: a mistakenly embedded credential must not leak.
        raise ConfigError(f'{path}: invalid YAML') from exc
    return validate(data)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def range_fingerprint(config):
    return canonical_hash(as_written({k: config[k] for k in ('project', 'anchor', 'range')}, include_defaults=True))


def runner_inputs(runner):
    """The same result-affecting runner view for stage and evidence caches."""
    data = {k: v for k, v in runner.items() if k not in ('workers', 'reuse')}
    if data.get('javac_mode') == 'subprocess':
        data.pop('javac_mode')  # Keep old result fingerprints for the unchanged default.
    if 'docker' in data:
        data['docker'] = {k: v for k, v in data['docker'].items() if k not in ('mounts', 'env_file')}
        if not data['docker']:
            data.pop('docker')
    return data


def record_runner_inputs(runner):
    """Normalize operational values to legacy constants for cache compatibility.

    Fixed placeholders retain caches created with the old default runner; their
    values never depend on the experiment's scheduling or host-path settings.
    """
    data = runner_inputs(runner)
    data.update(workers={k: 4 for k in ('build', 'survive', 'cover')},
                reuse={'checkouts': None, 'classpath_cache': None})
    data['docker'] = {**data.get('docker', {}), 'mounts': {}, 'env_file': None}
    return data


def result_inputs(config):
    """Hash result-affecting settings, excluding scheduling and host wiring."""
    c = as_written(config)
    c.pop('policies', None)
    for g in c.get('dev', {}).get('groups', []):
        if g.get('frozen', True):
            g.pop('frozen', None)
    if 'runner' in c:
        c['runner'] = runner_inputs(c['runner'])
        if not c['runner']:
            c.pop('runner')
    for g in c.get('generators', []):
        if g.get('docker') is not None:
            g['docker'] = {k: v for k, v in g['docker'].items() if k not in ('mounts', 'env_file')}
            if not g['docker']:
                g.pop('docker')
    model = c.get('model', {})
    model.pop('env_file', None)
    if 'model' in c and not model:
        c.pop('model')
    return c


def stage_inputs(c, stage, group=None, *, legacy=False):
    if stage not in STAGES:
        raise ConfigError(f'stage: allowed values: {", ".join(STAGES)}')
    index = STAGES.index(stage)
    c = as_written(c) if legacy else result_inputs(c)
    for g in c.get('dev', {}).get('groups', []):
        if index <= 3 or g.get('frozen', True):
            g.pop('frozen', None)
    data = {k: c[k] for k in ('project', 'anchor', 'range', 'runner') if k in c}
    baseline = {k: c['dev'][k] for k in ('stable_runs', 'unmapped') if k in c.get('dev', {})}
    if baseline:
        data['dev'] = baseline
    if index >= 1:
        data.update({k: c[k] for k in ('scope', 'dev') if k in c})
        if any(g.get('denominator') == 'coverage-scoped' for g in c.get('dev', {}).get('groups', [])) and 'environment_errors' in c.get('survival', {}):
            data['survival'] = {'environment_errors': c['survival']['environment_errors']}
        if 'generators' in c:
            data['generators'] = [{k: g[k] for k in ('name', 'method') if k in g} for g in c['generators']]
    if index >= 2:
        selected = [g for g in c.get('generators', []) if group is None or g['name'] == group]
        if group is not None and not selected:
            raise ConfigError('group: expected a configured generator name')
        if 'model' in c:
            data['model'] = c['model']
        if 'generators' in c:
            data['generators'] = selected
    if index >= 3 and 'screen' in c:
        data['screen'] = c['screen']
    if index >= 5 and 'survival' in c:
        data['survival'] = c['survival']
    if index >= 6 and 'coverage' in c:
        data['coverage'] = c['coverage']
    if index >= 7:
        data = c
    return data


def stage_hash(c, stage, group=None):
    return canonical_hash(stage_inputs(c, stage, group))


def legacy_stage_hash(c, stage, group=None):
    """Recognize pre-migration state without silently blessing changed results."""
    return canonical_hash(stage_inputs(c, stage, group, legacy=True))


def execution_groups(config, frozen):
    """Filter old screen artifacts without changing their selection evidence."""
    excluded = {g['name'] for g in config['dev']['groups'] if not g.get('frozen', True)}
    return {g: rows for g, rows in frozen.items() if g not in excluded}


def derived_source(config, group):
    groups = {g['name']: g for g in config['dev']['groups']}
    source = groups.get(group, {}).get('derive_from')
    return source if source and groups[source].get('frozen', True) else None
