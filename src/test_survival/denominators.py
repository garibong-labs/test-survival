"""Screen evidence and the single post-control population view."""
from .io import read_json, write_json, write_csv, atomic_text


def control_excluded(exp, config):
    if config['survival'].get('control_failures', 'stop') != 'exclude':
        return {}
    return read_json(exp/'results/control-excluded.json', {})


def frozen_after_control(exp, config, default=None):
    """Never mutate screen evidence; exclusions are group-specific method IDs."""
    frozen = read_json(exp/'results/frozen-denominators.json', default)
    if frozen is None:
        return None
    excluded = control_excluded(exp, config)
    omitted = {g: set(ids) for g, ids in excluded.items()}
    return {g: [t for t in rows if t['id'] not in omitted.get(g, set())]
            for g, rows in frozen.items()}


def denominator_counts(exp, config):
    frozen = frozen_after_control(exp, config)
    return ({g: len(rows) for g, rows in frozen.items()} if frozen is not None
            else read_json(exp/'results/denominators.json'))


def finish_control(exp, config, records):
    """Save the complete control observations before applying the admission rule."""
    write_json(exp/'results/control-at-t.json', records)
    lines = ['# Control at t', '']
    exclude = config['survival'].get('control_failures', 'stop') == 'exclude'
    if exclude:
        frozen = read_json(exp/'results/frozen-denominators.json')
        excluded = {g: [r['id'] for r in records.get(g, {}).get('failures', [])] for g in frozen}
        write_json(exp/'results/control-excluded.json', excluded)
        audit = []
        for g, record in records.items():
            for row in record['failures']:
                message = row.get('message', '').splitlines()
                audit.append(dict(group=g, **{k: row.get(k, '') for k in ('class', 'method', 'file', 'state')},
                                  message=message[0] if message else ''))
        write_csv(exp/'results/control-excluded.csv', audit, ['group', 'class', 'method', 'file', 'state', 'message'])
    counts = denominator_counts(exp, config)
    write_json(exp/'results/denominators.json', counts)
    for g, record in records.items():
        lines.append(f'- {g}: {record["passed"]}/{record["requested"]} passed.')
        if exclude:
            lines.append(f'  excluded after control at t: {len(excluded[g])}; denominator used: {counts[g]}')
    atomic_text(exp/'results/control-at-t.md', '\n'.join(lines)+'\n')
    if not exclude and any(r['failures'] for r in records.values()):
        raise RuntimeError('Frozen methods failed the control at t')
    return sum(len(r['failures']) for r in records.values()) if exclude else 0


def control_policy_current(exp, config):
    """The admission setting is deliberately outside the control stage hash."""
    records = read_json(exp/'results/control-at-t.json')
    if records is None:
        return True
    if config['survival'].get('control_failures', 'stop') == 'stop':
        return not any(r['failures'] for r in records.values())
    frozen = read_json(exp/'results/frozen-denominators.json', {})
    expected = {g: [t['id'] for t in records.get(g, {}).get('failures', [])] for g in frozen}
    return read_json(exp/'results/control-excluded.json') == expected
