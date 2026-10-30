"""Policy configuration is separate from the baseline stage family."""
from copy import deepcopy
import re

from ..adapters.policy_coverage import COVERAGE_VERSION
from ..config import ConfigError, Field, _validate, _strings, enum, integer
from .segments import identity

SCHEMA = {
    'baseline': Field(str), 'baseline_config': Field(str, None, nullable=True),
    'regenerate_developer': Field(bool, True), 'groups': Field(list), 'releases': Field(list, []),
    'threshold': Field(float, .5, minimum=0), 'max_threshold_triggers': integer(1, 0),
    'generator_overrides': Field(dict, {}), 'concurrency': Field(dict, {}),
    'fill_seed': integer(20261029, 0), 'set': Field(list),
}


def validate(data):
    c = _validate(data, SCHEMA, 'policies.')
    for key in ('groups', 'releases'):
        _strings(c[key], 'policies.' + key)
    if not c['groups'] or not c['set']:
        raise ConfigError('policies.groups and policies.set must be nonempty')
    if not 0 < c['threshold'] <= 1:
        raise ConfigError('policies.threshold must be in (0, 1]')
    names = {'none'}
    for i, policy in enumerate(c['set']):
        policy = _validate(policy, {'name': Field(str), 'trigger': enum('release', 'release', 'threshold'),
                                    'action': enum('reset', 'reset', 'fill')}, f'policies.set[{i}].')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', policy['name']) or policy['name'] in names:
            raise ConfigError('policies.set: names must be unique safe path components, excluding none')
        names.add(policy['name']); c['set'][i] = policy
    for name, value in c['concurrency'].items():
        if name not in [*c['groups'], 'dev_selection'] or type(value) is not int or value < 1:
            raise ConfigError('policies.concurrency: expected group/dev_selection -> positive integer')
    for name, value in c['generator_overrides'].items():
        if name not in c['groups'] or not isinstance(value, dict) or {'name', 'method', 'impl'} & value.keys():
            raise ConfigError('policies.generator_overrides: expected settings for selected generators; identity cannot change')
    return c


def merge(base, override):
    result = deepcopy(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else deepcopy(value)
    return result


def digest_options(options):
    # Execution/reporting filter: retain fingerprints of expensive existing pools.
    return {k:v for k,v in options.items() if k != 'regenerate_developer'}


def regenerates(b, group):
    return b.options.get('regenerate_developer', True) or group not in b.dev


def active_plan(b, plan):
    return {**plan, **{k:[p for p in plan.get(k, []) if regenerates(b, p['group'])]
                       for k in ('points', 'pools')}}


def reusable_run(record, *, required_methods=None):
    # A record may mix imported markers and real observations. Its imported
    # flag cannot establish whether it contains execution for current demand.
    if required_methods is not None and any(t.get('state') in ('dead', 'not-selected') and identity(t) in required_methods
                                            for t in record.get('methods', [])):
        return False
    # Imports and carry records contain no new execution to upgrade. In
    # particular, the interrupted JSV run has only imports at its pool index.
    return (record.get('coverage_version') == COVERAGE_VERSION
            or (record.get('coverage_version') == 2 and record.get('coverage_status') == 'complete')
            or record.get('imported') is True
            or record.get('status') in ('skipped-untouched', 'build-fail'))
