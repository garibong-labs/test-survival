"""Compute changed-scope-line coverage from survival evidence; never launch Java."""
from ..io import read_csv, read_json, write_csv, write_json
from .config import regenerates
from .segments import execution_id, identity


def run(b, step):
    policy_names = [p['name'] for p in b.options['set']]
    scope = {r['file'] for r in b.scope['classes']}
    changes = read_csv(b.path/'results/changed-lines.csv')
    for p in policy_names:
        output = b.exp/'results/policy'/p
        alive = read_json(output/'alive.json')
        segments = read_json(output/'segments.json')
        rows = []
        missing_path = output/'coverage-missing.csv'
        missing = [r for r in read_csv(missing_path) if r['reason'] == 'batch JVM failed or timed out'] if missing_path.exists() else []
        batch_missing = {(int(r['index']),r['group']) for r in missing}
        references = [g for g in b.groups if not regenerates(b,g)]
        for change_row in changes:
            index = int(change_row['index'])
            if not change_row.get('scope_executable') or int(change_row['scope_executable']) == 0 or not b.evaluable(index):
                continue
            change = read_json(b.path/f'results/changes/{index}.json')
            if change is None:
                raise ValueError('Missing changed-line image at '+str(index))
            changed = {(f,int(n)) for f,ns in change['post_image'].items() if f in scope for n in ns}
            universe_path = next((b.path/f'results/coverage/{index}-{g}-lines.csv' for g in b.groups
                                  if (b.path/f'results/coverage/{index}-{g}-lines.csv').exists()), None)
            executable = changed & {(r['file'],int(r['line'])) for r in read_csv(universe_path)} if universe_path else changed
            if len(executable) != int(change_row['scope_executable']):
                raise ValueError('Changed executable scope lines differ from baseline at '+str(index))
            row = {k:change_row[k] for k in ('index','sha','date','subject')}
            row['scope_executable'] = len(executable)
            cache = {}
            for g in b.groups:
                baseline = g in references or not any(int(s['start']) <= index for s in segments[g][1:])
                if baseline:
                    value = change_row.get('scope_'+g+'_covered', '')
                    covered = int(value) if value != '' else ''
                else:
                    hits = set(); reasons = {'batch JVM failed or timed out'} if (index,g) in batch_missing else set()
                    for t in alive[str(index)][g]:
                        digest = execution_id(t)
                        if digest not in cache:
                            cache[digest] = read_json(b.exp/f'results/policy/runs/{digest}/{index}.json', {})
                        raw = cache[digest]
                        found = [r for r in raw.get('methods', []) if identity(r) == identity(t)]
                        if not found or 'lines' not in found[0]:
                            reasons.add(raw.get('coverage_reason') or 'missing per-test coverage')
                        else:
                            hits.update((f,int(n)) for f,n in found[0]['lines'] if f in scope)
                    covered = '' if reasons else len(hits & executable)
                    if reasons and (index,g) not in batch_missing:
                        missing.append(dict(index=index, sha=row['sha'], group=g, reason='; '.join(sorted(reasons))))
                row[g+'_covered'] = covered
                row[g+'_percent'] = round(100*covered/len(executable),3) if covered != '' and executable else ''
                if g in references:
                    row[g+'_reference'] = 'baseline (no regeneration)'
            rows.append(row)
        fields = ['index','sha','date','subject','scope_executable',*[g+s for g in b.groups for s in ('_covered','_percent')],
                  *[g+'_reference' for g in references]]
        write_csv(output/'coverage-commits.csv',rows,fields)
        # Keep the report's total format; per-group denominators make missing
        # measurements explicit rather than silently presenting them as zero.
        measured = {g:[r for r in rows if r[g+'_covered'] != ''] for g in b.groups}
        total = dict(executable=sum(r['scope_executable'] for r in rows),
                     groups={g:sum(r[g+'_covered'] for r in measured[g]) for g in b.groups if measured[g] or not rows},
                     group_executable={g:sum(r['scope_executable'] for r in measured[g]) for g in b.groups},
                     reference_groups=references, missing_indices=sorted({int(r['index']) for r in missing}))
        write_json(output/'coverage-total.json',total)
        write_csv(output/'coverage-missing.csv',missing,['index','sha','group','reason'])
