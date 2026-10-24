"""Stable developer populations; class-name matching follows the archived rule."""

def denominator(methods, mode, scope, production_names):
    def targeted(cls):
        pkg, _, simple = cls.rpartition('.')
        for selected in scope:
            cp, _, name = selected['class'].rpartition('.')
            if pkg != cp or not simple.startswith(name) or not simple.endswith('Test'):
                continue
            suffix = simple[len(name):-4]
            if not suffix or suffix not in production_names:
                return True
        return False
    return [{k:r[k] for k in ('class','method','kind')} for r in methods
            if r['stable']=='yes' and (mode=='project' or targeted(r['class']))]


def coverage_scoped(methods, coverage):
    observations={(r['class'],r['method']):r for r in coverage}
    if len(observations)!=len(coverage):
        raise ValueError('Duplicate per-method coverage observation')
    kept,excluded=[],[]
    for row in methods:
        if row['stable']!='yes':continue
        method={k:row[k] for k in ['class','method','kind']}
        observation=observations.get((row['class'],row['method']))
        if observation is None:raise ValueError('Missing per-method coverage observation')
        count=int(observation['scope_lines_covered']);status=observation['status']
        if status=='pass' and count>0:kept.append(method)
        else:excluded.append({**method,'status':status,'scope_lines_covered':count,
                              'reason':'failed-alone' if status!='pass' else 'no-scope-line-covered'})
    return kept,excluded


def validate_derived_subsets(groups, denominators):
    for group in groups:
        source=group.get('derive_from')
        if source is None:continue
        parent={(r['class'],r['method']) for r in denominators[source]}
        child={(r['class'],r['method']) for r in denominators[group['name']]}
        if not child<=parent:
            raise ValueError('dev.groups.'+group['name']+'.derive_from: denominator must be a subset of '+source)


def derive_record(source, group, denominator):
    from copy import deepcopy
    wanted={r['id'] for r in denominator}
    if source['status']=='skipped-untouched':
        return {**source,'group':group,'derived_from':source['group']}
    available={r['id'] for r in source['methods']}
    if not wanted<=available:raise ValueError('Derived denominator is absent from source raw methods')
    result=deepcopy(source)
    result.update(group=group,derived_from=source['group'],requested_ids=[r['id'] for r in denominator],
                  methods=[m for m in result['methods'] if m['id'] in wanted])
    result['files']=[{**part,'methods':[m for m in part['methods'] if m['id'] in wanted]}
                     for part in result['files'] if any(m['id'] in wanted for m in part['methods'])]
    return result
