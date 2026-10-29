"""Pure composition and aggregation rules, independent of Java execution."""
from collections import Counter, defaultdict
import random

from ..config import canonical_hash
from ..rules.survival import advance


def identity(test):
    return test['class']+'#'+test['method']


def key(test):
    return test['file_hash']+'|'+identity(test)


def segment(number, index, carried, new):
    members = [dict(t, composition='carried') for t in carried] + [dict(t, composition='new') for t in new]
    if len({key(t) for t in members}) != len(members):
        raise ValueError('Duplicate test in segment')
    return dict(segment=number, start=index, segment_denominator=len(members), carried=[key(t) for t in carried],
                new=[key(t) for t in new], tests=members)


def regenerate(current, alive, pool, *, action, developer, seed, index):
    survivors = [t for t in current['tests'] if key(t) in alive]
    if action == 'reset':
        return segment(current['segment']+1, index, [], pool)
    if action != 'fill':
        raise ValueError('Unknown regeneration action')
    if developer:
        ids = {identity(t) for t in survivors}
        replacements = [t for t in pool if identity(t) not in ids]
    else:
        broken = Counter(t['unit'] for t in current['tests'] if key(t) not in alive)
        choices = defaultdict(list)
        surviving = {key(t) for t in survivors}
        for t in pool:
            if key(t) not in surviving:
                choices[t['unit']].append(t)
        replacements = []
        for unit, count in sorted(broken.items()):
            candidates = sorted(choices[unit], key=lambda t: (t['id'], key(t)))
            rng = random.Random(canonical_hash([seed, index, unit]))
            replacements.extend(rng.sample(candidates, min(count, len(candidates))))
    return segment(current['segment']+1, index, survivors, replacements)


def assemble(tests, runs):
    """Map shared physical method results onto the selected segment members."""
    result = []
    for t in tests:
        run = runs[t['file_hash']]
        matches = [r for r in run['methods'] if identity(r) == identity(t)]
        if len(matches) != 1:
            raise ValueError('Missing or ambiguous shared run method: '+key(t))
        result.append({**matches[0], 'id': key(t)})
    return result


def evaluate(current, alive, observations, evaluated=True):
    wanted = {key(t) for t in current['tests']}
    if evaluated and {r['id'] for r in observations} != wanted:
        raise ValueError('Run methods differ from segment denominator')
    remaining, lost = advance(alive, observations, evaluated=evaluated)
    carried = set(current['carried'])
    return remaining, lost, dict(segment=current['segment'], segment_denominator=current['segment_denominator'],
                                alive=len(remaining), lost=len(lost), carried_alive=len(remaining & carried),
                                new_alive=len(remaining - carried))
