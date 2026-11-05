"""Policy configuration is separate from the baseline stage family."""
from copy import deepcopy
import re

from ..adapters.policy_coverage import COVERAGE_VERSION
from ..config import ConfigError, Field, _validate, _strings, enum, integer
from ..rules.coverage import usable_lines
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
        supplied_developer = 'developer' in policy
        policy = _validate(policy, {'name': Field(str), 'trigger': enum('release', 'release', 'threshold'),
                                    'action': enum('reset', 'reset', 'fill'),
                                    'developer': enum('regenerate' if c['regenerate_developer'] else 'keep', 'regenerate', 'keep')}, f'policies.set[{i}].')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', policy['name']) or policy['name'] in names:
            raise ConfigError('policies.set: names must be unique safe path components, excluding none')
        if not supplied_developer:
            policy.pop('developer')  # preserve old plan and config identities
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
    # Composition changes must not invalidate already published pools.
    return {k: ([{n:x for n,x in p.items() if n != 'developer'} for p in v] if k == 'set' else v)
            for k,v in options.items() if k != 'regenerate_developer'}


def digest_plan(plan):
    if 'policies' not in plan:
        return plan
    return {**plan, 'policies':[{k:v for k,v in p.items() if k != 'developer'} for p in plan['policies']]}


def regenerates(b, group, policy=None):
    if group not in b.dev:
        return True
    if isinstance(policy, str):
        if policy == 'none':
            return False
        policy = next(p for p in b.options['set'] if p['name'] == policy)
    if policy is not None:
        return policy.get('developer', 'regenerate' if b.options.get('regenerate_developer', True) else 'keep') == 'regenerate'
    return any(regenerates(b, group, p) for p in b.options['set']) if 'set' in b.options else b.options.get('regenerate_developer', True)


def requested_point(b, point):
    return any(p['trigger'] == point['trigger'] and regenerates(b,point['group'],p) for p in b.options['set'])


def active_plan(b, plan):
    if 'points' not in plan:
        return {**plan, 'pools':[p for p in plan.get('pools', []) if regenerates(b,p['group'])]}
    points = [p for p in plan.get('points', []) if requested_point(b,p)]
    needed = {(p['group'],p['index']) for p in points if p['index'] is not None}
    return {**plan, 'points':points, 'pools':[p for p in plan.get('pools', [])
            if p['group'] not in b.dev or (p['group'],p['index']) in needed]}


MARKERS = ('dead', 'not-selected')  # record rows that stand for an absent execution


def unmet_rows(record, required_methods):
    """The rows of required members that do not hold a usable observation (a
    marker, or no executed lines); None when a row is malformed."""
    rows = []
    for t in record.get('methods', []):
        if 'class' not in t or 'method' not in t:
            return None  # a malformed or legacy row is replaced, never a crash at the gate
        if identity(t) in required_methods and (t.get('state') in MARKERS or not usable_lines(t)):
            rows.append(t)
    return rows


def reusable_run(record, *, required_methods=None):
    # A method the policies need must carry its executed lines: records an
    # older version imported from the baseline, pool-screening markers and
    # chunks whose coverage JVM failed have none, and are measured again
    # (once per run, by the measuring sites).
    if required_methods is not None and unmet_rows(record, required_methods) != []:
        return False
    # Carry records contain no execution to upgrade.
    return (record.get('coverage_version') == COVERAGE_VERSION
            or (record.get('coverage_version') == 2 and record.get('coverage_status') == 'complete')
            or record.get('status') in ('skipped-untouched', 'build-fail'))
