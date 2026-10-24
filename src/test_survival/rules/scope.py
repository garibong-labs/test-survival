"""Scope selection on immutable inventory rows."""

def select_scope(survey, config):
    eligible = [dict(r) for r in survey if int(r['m']) >= config['min_methods'] and
                (not config['exclude_accessor_heavy'] or int(r.get('accessors', 0))*2 <= int(r['m']))]
    if config['rule'] == 'explicit':
        by_name = {r['class']: r for r in survey}
        missing = set(config['classes']) - by_name.keys()
        if missing:
            raise ValueError('scope.classes: classes absent from inventory: '+', '.join(sorted(missing)))
        chosen = [dict(by_name[n]) for n in config['classes']]
    else:
        if config['rule'] == 'dedicated-test':
            eligible = [r for r in eligible if r['has_test']=='yes']
        metric = {'change-count': 'c', 'method-count': 'm'}[config.get('rank_by', 'change-count')]
        ranked = sorted(eligible, key=lambda r: (-int(r[metric]), r['class']))
        chosen, total = [], 0
        for row in ranked:
            if total + int(row['m']) > config['target_methods'][1]:
                break
            chosen.append(row)
            total += int(row['m'])
            if total >= config['target_methods'][0] or len(chosen) == config['max_classes']:
                break
    return chosen, len(eligible)
