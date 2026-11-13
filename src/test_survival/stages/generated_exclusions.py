"""Filter already screened, deduplicated candidates and disclose the decisions."""
import json

from ..io import atomic_text, read_csv, read_json, write_csv, write_json
from ..importer import rounds
from ..rules.generated_exclusions import matches


FIELDS = ['group', 'file', 'class', 'method', 'selector', 'screening_state', 'prior_admission', 'reason']


def identity(test):
    return tuple(test.get(k, '') for k in ('file', 'class', 'method'))


def apply_exclusions(exp, config, group, kept):
    """Never promote a duplicate when an earlier accepted body is excluded."""
    name = group['name']
    selectors = config['screen'].get('exclude', {}).get(name, [])
    removed = [t for t in kept if any(matches(s, t, name, group['method']) for s in selectors)]
    denied = {identity(t) for t in removed}
    retained = [t for t in kept if identity(t) not in denied]
    audit = []; matched = set(); recorded = set()
    directories = rounds(exp/'generated'/name)
    for directory in directories:
        for path in sorted((directory/'validation').glob('*.json')) if selectors else []:
            record = read_json(path)
            tests = record.get('methods') or [dict(file=record['file'], state='unparsed')]
            for test in tests:
                selected = [s for s in selectors if matches(s, test, name, group['method'])]
                if not selected:
                    continue
                matched.update(selected); recorded.add(identity(test))
                audit.append(dict(group=name, file=test['file'], **{'class':test.get('class', '')},
                                  method=test.get('method', ''), selector=';'.join(selected),
                                  screening_state=test.get('state', ''),
                                  prior_admission='yes' if identity(test) in denied else 'no',
                                  reason='excluded-by-configuration'))
    for test in removed:
        if identity(test) not in recorded:
            selected = [s for s in selectors if matches(s, test, name, group['method'])]
            matched.update(selected)
            audit.append(dict(group=name, **{k:test[k] for k in ('file', 'class', 'method')},
                              selector=';'.join(selected), screening_state=test.get('state', 'pass'),
                              prior_admission='yes', reason='excluded-by-configuration'))
    summary_path = exp/'results/generated-exclusions.json'
    if 'exclude' in config['screen'] or summary_path.exists():
        csv = exp/'results/generated-exclusions.csv'
        previous = read_csv(csv) if csv.exists() else []
        write_csv(csv, [r for r in previous if r['group'] != name] + audit, FIELDS)
        summary = read_json(summary_path, {})
        summary[name] = dict(before=len(kept), excluded=len(removed), after=len(retained),
                             selectors=selectors, unmatched=[s for s in selectors if s not in matched])
        write_json(summary_path, summary)
    # Round funnels retain the original stability/dedup counts and add an
    # exclusion overlay. Generation stopping evidence is left untouched.
    through = set()
    for directory in directories:
        funnel = read_json(directory/'funnel.json')
        if funnel is None:
            continue
        through.add(directory.name)
        current = [t for t in removed if t['round'] == directory.name]
        if 'exclude' in config['screen'] or 'configured_excluded' in funnel:
            cumulative = sum(t['round'] in through for t in removed)
            funnel.update(configured_excluded=len(current), configured_excluded_total=cumulative,
                          final_kept=funnel['total_kept']-cumulative)
            for technique, values in funnel.get('per_prompt', {}).items():
                count = sum(t['technique'] == technique for t in current)
                values.update(configured_excluded=count, final_kept=values['kept']-count)
            write_json(directory/'funnel.json', funnel)
            atomic_text(directory/'funnel.md', '# Generation funnel\n\n```json\n'+json.dumps(funnel, indent=2)+'\n```\n')
    return retained


def exclusion_notes(summary):
    return [f'{group}: screen.exclude removed {row["excluded"]} of {row["before"]} '
            f'deduplicated generated methods; {row["after"]} remain before denominator matching. '
            'See results/generated-exclusions.csv.' for group, row in summary.items() if row['selectors']]
