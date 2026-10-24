"""Deterministic method sampling and collision-only KTester ID repair."""
from collections import Counter, defaultdict
from copy import deepcopy
import random


def subsample(rows, count, group):
    if len(rows) <= count:
        return list(rows)
    selected = set(random.Random(group).sample(range(len(rows)), count))
    return [r for i,r in enumerate(rows) if i in selected]


def renumber(entries):
    result = deepcopy(entries)
    frequencies = Counter(m['id'] for m in result)
    reserved = {key for key,n in frequencies.items() if n==1}
    groups = defaultdict(list)
    for entry in result:
        if frequencies[entry['id']] > 1:
            groups[entry['class'],entry['method-name'].split('(',1)[0]].append(entry)
    def order(entry):
        params = entry['method-name'].split('(',1)[1][:-1]
        return len(params.split(', ')) if params else 0, params
    for (cls, method), group in sorted(groups.items()):
        if len({r['method-name'] for r in group}) != len(group):
            raise ValueError('Duplicate focal signatures')
        base = cls.rsplit('.',1)[-1]+'_'+method
        index = 1
        for entry in sorted(group, key=order):
            while True:
                ident = base + ('' if index==1 else '_'+str(index))
                index += 1
                if ident not in reserved:
                    break
            reserved.add(ident)
            if ident != entry['id']:
                entry['id'] = ident
                entry['test-class'] = entry['package']+'.'+ident+'_Test'
                entry['test-path'] = 'src/test/java/'+entry['test-class'].replace('.','/')+'.java'
    return result
