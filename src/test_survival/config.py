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


GENERATOR = {
    'name': Field(str), 'display': Field(str, None, nullable=True),
    'method': enum('ktester', 'ktester', 'ouedraogo', 'testpilot'),
    'impl': enum('author-tool', 'author-tool', 'native'),
    'tool_path': Field(str, None, nullable=True), 'params': Field(dict, {}),
    'rounds': {'max': integer(1), 'until_dev_matched': Field(bool, False),
               'retry_on_zero_compile': integer(0, 0), 'stop_after_zero_compile': integer(0, 0)},
    'context': {'dependency_signatures': {'enabled': Field(bool, False), 'max_types': integer(15), 'max_lines': integer(400)}},
    'prompt': {'api_constraint': Field(bool, False)},
    'postprocess': {'import_repair': {'enabled': Field(bool, False), 'passes': integer(2)}},
}
SCHEMA = {
    'name': Field(str),
    'project': {'repo': Field(str), 'language': enum('java', 'java', 'javascript'),
                'build': enum('maven', 'maven', 'npm', 'pnpm'), 'module': Field(str, '.')},
    'anchor': Field(str),
    'range': {'branch': Field(str, 'origin/master'), 'end': Field(str, None, nullable=True),
              'mode': enum('first-parent', 'first-parent'), 'limit': integer(None, 1, True)},
    'scope': {'rule': enum('change-ranked', 'dedicated-test', 'change-ranked', 'explicit'),
              'min_methods': integer(3), 'exclude_accessor_heavy': Field(bool, True),
              'target_methods': Field(list, [50, 80]), 'max_classes': integer(10),
              'classes': Field(list, []), 'min_classes': integer(1)},
    'dev': {'denominator': enum('project', 'project', 'scope-targeted'), 'stable_runs': integer(3)},
    'model': {'provider': enum('openrouter', 'openrouter'), 'name': Field(str, 'openai/gpt-oss-120b'),
              'env_file': Field(str, '/Users/donggi/_projects/experiment-projects/KTester/docker/.env'),
              'env_key': Field(str, 'OPENROUTER_API_KEY')},
    'generators': Field(list, []),
    'screen': {'stable_runs': integer(3), 'dedup': enum('exact-body', 'exact-body'),
               'matching': enum('none', 'none', 'subsample-to-dev')},
    'survival': {'definition': enum('cumulative-fixed-denominator', 'cumulative-fixed-denominator'),
                 'flaky_rerun': integer(1, 0), 'build_fail': enum('carry-over', 'carry-over'),
                 'dev_cache': enum('none', 'none', 'unchanged-module')},
    'coverage': {'changed_line': Field(bool, True), 'at_anchor': Field(bool, True),
                 'groups': enum('alive-entering', 'alive-entering')},
    'runner': {
        'docker': {'compose_files': Field(list, []), 'service': Field(str, 'ktester'),
                   'image': Field(str, None, nullable=True), 'mounts': Field(dict, {}),
                   'env_file': Field(str, None, nullable=True)},
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


def validate(data):
    c = _validate(data, SCHEMA)
    if (c['project']['language'], c['project']['build']) != ('java', 'maven'):
        raise ConfigError('project.language/project.build: not implemented in this version of the tool; implemented: java/maven')
    module = Path(c['project']['module'])
    if module.is_absolute() or '..' in module.parts:
        raise ConfigError('project.module: expected a relative path without ..')
    targets = c['scope']['target_methods']
    if len(targets) != 2 or any(type(n) is not int or n < 1 for n in targets) or targets[0] > targets[1]:
        raise ConfigError('scope.target_methods: expected [positive minimum, maximum >= minimum]')
    if c['scope']['min_classes'] > c['scope']['max_classes']:
        raise ConfigError('scope.min_classes: must be <= scope.max_classes')
    _strings(c['scope']['classes'], 'scope.classes')
    if c['scope']['rule'] == 'explicit' and not c['scope']['classes']:
        raise ConfigError('scope.classes: required nonempty list for explicit scope')
    generators = []
    names = {'dev'}
    for i, raw in enumerate(c['generators']):
        prefix = f'generators[{i}].'
        g = _validate(raw, GENERATOR, prefix)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', g['name']) or g['name'] in names:
            raise ConfigError(prefix + 'name: must be unique, not dev, and contain only letters, digits, _ or -')
        names.add(g['name'])
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
    docker = c['runner']['docker']
    _strings(docker['compose_files'], 'runner.docker.compose_files')
    if docker['compose_files'] and docker['image']:
        raise ConfigError('runner.docker: compose_files and image are alternatives')
    for host, target in docker['mounts'].items():
        if not isinstance(host, str) or not isinstance(target, str) or not Path(host).is_absolute() or not target.startswith('/'):
            raise ConfigError('runner.docker.mounts: expected absolute host path -> absolute container path')
    for key, value in c['runner']['jvm'].items():
        if not re.fullmatch(r'[1-9][0-9]*[kKmMgG]?', value):
            raise ConfigError(f'runner.jvm.{key}: expected a positive JVM memory size, such as 512m or 1g')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', c['model']['env_key']):
        raise ConfigError('model.env_key: expected an environment variable name')
    return c


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
    path = Path(path)
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


def stage_inputs(c, stage, group=None):
    if stage not in STAGES:
        raise ConfigError(f'stage: allowed values: {", ".join(STAGES)}')
    index = STAGES.index(stage)
    data = {k: c[k] for k in ('project', 'anchor', 'range', 'runner')}
    data['dev'] = {'stable_runs': c['dev']['stable_runs']}
    if index >= 1:
        data.update(scope=c['scope'], dev=c['dev'], generators=[{k: g[k] for k in ('name', 'method')} for g in c['generators']])
    if index >= 2:
        selected = [g for g in c['generators'] if group is None or g['name'] == group]
        if group is not None and not selected:
            raise ConfigError('group: expected a configured generator name')
        data.update(model=c['model'], generators=selected)
    if index >= 3:
        data['screen'] = c['screen']
    if index >= 5:
        data['survival'] = c['survival']
    if index >= 6:
        data['coverage'] = c['coverage']
    if index >= 7:
        data = c
    return data


def stage_hash(c, stage, group=None):
    return canonical_hash(stage_inputs(c, stage, group))
