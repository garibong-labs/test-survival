"""Read saved stage outputs for the CLI without running experiment work."""
from .io import read_csv, read_json


def table(headers, rows):
    values = [[str(v) for v in row] for row in [headers, *rows]]
    widths = [max(len(row[i]) for row in values) for i in range(len(headers))]
    lines = ['  '.join(v.ljust(widths[i]) for i, v in enumerate(row)).rstrip() for row in values]
    lines.insert(1, '  '.join('-' * width for width in widths))
    return '\n'.join(lines)


def show(exp, stage):
    state = read_json(exp/'state'/f'{stage}.json', {})
    lines = [f'{stage}: {state.get("status", "pending")}']
    if state.get('notes'):
        lines.append(str(state['notes']))
    headers, rows = ['Output', 'Value'], []
    if stage == 'prepare':
        anchor = read_json(exp/'results/anchor.json', {})
        rows = [(key, anchor[key]) for key in ['tag', 'sha', 'origin_master', 'n', 'key_check'] if key in anchor]
    elif stage == 'scope':
        headers = ['Class', 'Focal methods', 'Changed commits']
        rows = [(r['class'], r['m'], r['c']) for r in read_json(exp/'population/scope.json', {}).get('classes', [])]
    elif stage == 'generate':
        headers = ['Group', 'Rounds', 'Stopped']
        for path in sorted((exp/'generated').glob('*/rounds.json')):
            record = read_json(path)
            rows.append((path.parent.name, len(record.get('rounds', [])), record.get('stopped', '')))
    elif stage in ['screen', 'control', 'survive', 'report']:
        counts = read_json(exp/'results/denominators.json', {})
        headers = ['Group', 'Denominator']
        extra = {}
        if stage == 'control':
            headers.append('Passed at anchor')
            extra = {g:r['passed'] for g,r in read_json(exp/'results/control-at-t.json', {}).items()}
        elif stage in ['survive', 'report'] and (exp/'results/survival.csv').exists():
            curve = read_csv(exp/'results/survival.csv')
            if curve:
                headers.append('Alive at index '+curve[-1]['index'])
                extra = {g:curve[-1][g+'_alive'] for g in counts}
        rows = [(g, n, extra.get(g, 'pending')) if len(headers)==3 else (g, n) for g,n in counts.items()]
    elif stage == 'cover':
        record = read_json(exp/'results/coverage-consistency.json', {})
        rows = [(key, record[key]) for key in ['coverage_indices', 'production_changes', 'zero_executable_change_commits', 'unavailable_commits'] if key in record]
        if 'missing' in record:
            rows.append(('missing measurements', len(record['missing'])))
    lines.append(table(headers, rows) if rows else 'No saved summary outputs yet.')
    return '\n\n'.join(lines)
