"""Compute line coverage from survival evidence; never launch Java."""
from ..io import result_exists, read_csv, read_json, write_csv, write_json
from ..report import coverage_total
from ..rules.coverage import coordinates, usable_lines
from .config import regenerates
from .segments import execution_id, identity, key


def covered_lines(b, policy, group, segment, index, members, executable, cache, reasons):
    """Count each current coordinate once across the requested segment members."""
    affected = b.execution_config['survival'].get('selection') == 'affected'
    if affected:
        from .affected import logical_path
        raw = read_json(logical_path(b, policy, group, segment['segment'], index), {})
        logical = {key(row):row for row in raw.get('methods', [])}
    hits = set()
    for test in members:
        if affected:
            found = logical.get(key(test))
        else:
            digest = execution_id(test)
            if digest not in cache:
                raw = read_json(b.exp/f'results/policy/runs/{digest}/{index}.json', {})
                cache[digest] = raw, {identity(row):row for row in raw.get('methods', [])}
            raw, methods = cache[digest]
            found = methods.get(identity(test))
        if found is None or 'lines' not in found:
            reasons.add(raw.get('coverage_reason') or 'missing per-test coverage')
        elif not usable_lines(found):
            reasons.add('remeasure did not execute the method: '+str(found['remeasured_state']))
        else:
            hits.update((f,int(n)) for f,n in found['lines'])
    return '' if reasons else len(hits & executable)


def active_segment(segments, index):
    return next(s for s in reversed(segments) if int(s['start']) <= index)


def endpoint_totals(b, p, endpoints, segments, alive, missing, batch_missing):
    totals = []
    universes = {}
    cache = {}
    for source in endpoints:
        row = dict(source)
        index, g = int(row['index']), row['group']
        active = active_segment(segments[g], index)
        if active['segment'] > 1:
            if index not in universes:
                universes[index] = coordinates(b.path, index, b.groups)
            members = alive[str(index if active['start'] == index else index-1)][g]
            reasons = set()
            covered = covered_lines(b,p,g,active,index,members,universes[index],cache,reasons)
            row['covered_lines'] = covered
            row['percent'] = round(100*covered/len(universes[index]),3) if covered != '' and universes[index] else ''
            if reasons and (index,g) not in batch_missing:
                missing.append(dict(index=index, sha=row['sha'], group=g, reason='; '.join(sorted(reasons))))
        totals.append(row)
    coverage_total.write(b.exp/'results/policy'/p, totals)


def run(b, step):
    policy_names = [p['name'] for p in b.options['set']]
    scope = {r['file'] for r in b.scope['classes']}
    production = b.execution_config.get('coverage', {}).get('universe') == 'production'
    partition = 'all' if production else 'scope'
    denominator = partition+'_executable'
    if production:
        endpoints = [r for r in read_csv(b.path/'results/coverage-total.csv') if r['group'] in b.groups]
        coverage_total.write(b.exp/'results/policy/none', endpoints)
    changes = read_csv(b.path/'results/changed-lines.csv')
    for p in policy_names:
        output = b.exp/'results/policy'/p
        alive = read_json(output/'alive.json')
        segments = read_json(output/'segments.json')
        rows = []
        missing_path = output/'coverage-missing.csv'
        # Survival lists every (index, group) whose rows lack lines, under the
        # record's reason; those entries are kept, whatever the reason and whether
        # or not the index changes scope lines, and this stage adds its own.
        missing = list(read_csv(missing_path)) if missing_path.exists() else []
        batch_missing = {(int(r['index']),r['group']):r['reason'] for r in missing}
        references = [g for g in b.groups if not regenerates(b,g,p)]
        for change_row in changes:
            index = int(change_row['index'])
            if not change_row.get(denominator) or int(change_row[denominator]) == 0 or not b.evaluable(index):
                continue
            change = read_json(b.path/f'results/changes/{index}.json')
            if change is None:
                raise ValueError('Missing changed-line image at '+str(index))
            changed = {(f,int(n)) for f,ns in change['post_image'].items() if production or f in scope for n in ns}
            universe_path = next((b.path/f'results/coverage/{index}-{g}-lines.csv' for g in b.groups
                                  if result_exists(b.path/f'results/coverage/{index}-{g}-lines.csv')), None)
            executable = changed & {(r['file'],int(r['line'])) for r in read_csv(universe_path)} if universe_path else changed
            if production and universe_path is None:
                executable = changed & coordinates(b.path, index, b.groups)
            if len(executable) != int(change_row[denominator]):
                raise ValueError('Changed executable '+partition+' lines differ from baseline at '+str(index))
            row = {k:change_row[k] for k in ('index','sha','date','subject')}
            row[denominator] = len(executable)
            cache = {}
            for g in b.groups:
                baseline = g in references or not any(int(s['start']) <= index for s in segments[g][1:])
                if baseline:
                    value = change_row.get(partition+'_'+g+'_covered', '')
                    covered = int(value) if value != '' else ''
                else:
                    reasons = {batch_missing[index,g]} if (index,g) in batch_missing else set()
                    active = active_segment(segments[g], index)
                    members = alive[str(index)][g]
                    if production and active['start'] != index:
                        members = alive[str(index-1)][g]
                    covered = covered_lines(b,p,g,active,index,members,executable,cache,reasons)
                    if reasons and (index,g) not in batch_missing:
                        reason = '; '.join(sorted(reasons))
                        batch_missing[index,g] = reason
                        missing.append(dict(index=index, sha=row['sha'], group=g, reason=reason))
                row[g+'_covered'] = covered
                row[g+'_percent'] = round(100*covered/len(executable),3) if covered != '' and executable else ''
                if g in references:
                    row[g+'_reference'] = 'baseline (no regeneration)'
            rows.append(row)
        fields = ['index','sha','date','subject',denominator,*[g+s for g in b.groups for s in ('_covered','_percent')],
                  *[g+'_reference' for g in references]]
        write_csv(output/'coverage-commits.csv',rows,fields)
        # Keep the report's total format; per-group denominators make missing
        # measurements explicit rather than silently presenting them as zero.
        measured = {g:[r for r in rows if r[g+'_covered'] != ''] for g in b.groups}
        total = dict(executable=sum(r[denominator] for r in rows),
                     groups={g:sum(r[g+'_covered'] for r in measured[g]) for g in b.groups if measured[g] or not rows},
                     group_executable={g:sum(r[denominator] for r in measured[g]) for g in b.groups},
                     reference_groups=references, missing_indices=sorted({int(r['index']) for r in missing}))
        if production:
            endpoint_totals(b, p, endpoints, segments, alive, missing, batch_missing)
            total['missing_indices'] = sorted({int(r['index']) for r in missing})
        write_json(output/'coverage-total.json',total)
        write_csv(output/'coverage-missing.csv',missing,['index','sha','group','reason'])
