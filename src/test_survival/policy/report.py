"""Segment-aware policy tables and a dependency-free SVG figure."""
from html import escape
import shutil

from ..io import read_csv, read_json, write_csv, write_json, atomic_text


def run(b):
    root = b.exp/'results/policy'
    none = root/'none'; none.mkdir(parents=True, exist_ok=True)
    shutil.copy2(b.path/'results/survival.csv',none/'survival.csv')
    shutil.copy2(b.path/'results/first-failures.csv',none/'first-failures.csv')
    changed = read_csv(b.path/'results/changed-lines.csv')
    baseline_coverage = [{**{k:r[k] for k in ('index','sha','date','subject','scope_executable')},
                          **{g+s:r.get('scope_'+g+s,'') for g in b.groups for s in ('_covered','_percent')}} for r in changed]
    write_csv(none/'coverage-commits.csv',baseline_coverage,['index','sha','date','subject','scope_executable',*[g+s for g in b.groups for s in ('_covered','_percent')]])
    write_json(none/'coverage-total.json',dict(executable=sum(int(r['scope_executable']) for r in changed if r['scope_executable']),
                                             groups={g:sum(int(r.get('scope_'+g+'_covered') or 0) for r in changed) for g in b.groups}))
    names = ['none',*[p['name'] for p in b.options['set']]]
    summary = []; checks = []; curves = {}
    for p in names:
        curves[p] = read_csv(root/p/'survival.csv')
        segments = read_json(root/p/'segments.json') if p != 'none' else None
        total = read_json(root/p/'coverage-total.json')
        row = dict(policy=p)
        table = [f'# {p}: survival by event', '', '| Index | Event | '+ ' | '.join(b.groups)+' |', '|---:|---|'+ '|'.join('---:' for _ in b.groups)+'|']
        for r in curves[p]:
            if r.get('event') == 'regenerate' or any(int(r[g+'_lost']) for g in b.groups) or int(r['index']) == 0 or (p != 'none' and any(int(s['start']) == int(r['index']) for gs in segments.values() for s in gs[1:])):
                cells = []
                for g in b.groups:
                    denominator = int(r[g+'_segment_denominator']) if p != 'none' else len(b.frozen[g])
                    n = int(r[g+'_alive']); cells.append(f'{n} ({100*n/denominator:.1f}%)' if denominator else '0 (—)')
                table.append(f'| {r["index"]} | {r.get("event","evaluate")} | '+ ' | '.join(cells)+' |')
        atomic_text(root/p/'table1-survival-by-event.md','\n'.join(table)+'\n')
        for g in b.groups:
            row[g+'_regenerations'] = len(segments[g])-1 if segments else 0
            row[g+'_entered_after_t'] = sum(len(s['new']) for s in segments[g][1:]) if segments else 0
            row[g+'_alive'] = int(curves[p][-1][g+'_alive'])
            row[g+'_segment_denominator'] = int(curves[p][-1][g+'_segment_denominator']) if segments else len(b.frozen[g])
            row[g+'_changed_lines_covered'] = total['groups'][g]
            row[g+'_changed_lines_executable'] = total['executable']
            previous = None
            for r in curves[p]:
                segment = int(r[g+'_segment']) if segments else 1
                denominator = int(r[g+'_segment_denominator']) if segments else len(b.frozen[g])
                n = int(r[g+'_alive'])
                if not 0 <= n <= denominator:
                    raise ValueError('Survival outside segment denominator')
                if previous and previous[0] == segment and (n > previous[1] or denominator != previous[2]):
                    raise ValueError('Segment survival increased or denominator changed')
                previous = (segment,n,denominator)
            if segments and (segments[g][0]['segment_denominator'] != len(b.frozen[g])):
                raise ValueError('Segment 1 differs from baseline')
        checks.append(f'- {p}: non-increasing within every segment; fixed denominators; segment 1 equals baseline.')
        summary.append(row)
    if (none/'survival.csv').read_bytes() != (b.path/'results/survival.csv').read_bytes():
        raise ValueError('none differs from baseline')
    checks.append('- none: survival.csv is byte-for-byte identical to the baseline.')
    needed = read_json(root/'needed-runs.json',[])
    missing = [r for r in needed if not (root/f'runs/{r["file_hash"]}/{r["index"]}.json').exists()]
    if missing:
        raise ValueError('Missing policy runs')
    checks.append(f'- Every needed shared run is present ({len(needed)} runs).')
    write_csv(root/'summary.csv',summary,list(summary[0]))
    lines = ['# Policy summary', '', '| Policy | Group | Regenerations | Entered after t | Final alive / denominator | Changed lines covered / executable |', '|---|---|---:|---:|---:|---:|']
    for r in summary:
        for g in b.groups:
            lines.append(f'| {r["policy"]} | {g} | {r[g+"_regenerations"]} | {r[g+"_entered_after_t"]} | {r[g+"_alive"]} / {r[g+"_segment_denominator"]} | {r[g+"_changed_lines_covered"]} / {r[g+"_changed_lines_executable"]} |')
    atomic_text(root/'summary.md','\n'.join(lines)+'\n')
    atomic_text(root/'consistency.md','# Consistency\n\n'+'\n'.join(checks)+'\n')
    figure(root,b,curves)


def figure(root,b,curves):
    colors = ['#475569','#2563eb','#ea580c','#059669','#9333ea','#db2777']
    height = 280*len(b.groups)+70
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1100 {height}" role="img" aria-label="Alive test count by policy and commit">',
           '<rect width="100%" height="100%" fill="white"/><g font-family="sans-serif" font-size="14">']
    maximum = max(int(r['index']) for r in b.curves) or 1
    for number,g in enumerate(b.groups):
        top = 45+280*number; bottom = top+210
        ymax = max(int(r[g+'_alive']) for rows in curves.values() for r in rows) or 1
        svg.append(f'<text x="70" y="{top-15}">{escape(g)} — alive tests</text>')
        for tick in range(5):
            y = bottom-210*tick/4
            svg.append(f'<path d="M 70 {y} H 1060" stroke="#e2e8f0"/><text x="60" y="{y+5}" text-anchor="end">{round(ymax*tick/4)}</text>')
        for n,(p,rows) in enumerate(curves.items()):
            commands = []
            for i,r in enumerate(rows):
                x = 70+990*int(r['index'])/maximum; y = bottom-210*int(r[g+'_alive'])/ymax
                commands.append(f'M {x} {y}' if not i else f'H {x} V {y}')
            svg.append(f'<path d="{" ".join(commands)}" fill="none" stroke="{colors[n%len(colors)]}" stroke-width="2"/>')
        for tick in range(6):
            svg.append(f'<text x="{70+990*tick/5}" y="{bottom+22}" text-anchor="middle">{round(maximum*tick/5)}</text>')
    for n,p in enumerate(curves):
        svg.append(f'<text x="{70+n*200}" y="{height-20}" fill="{colors[n%len(colors)]}">{escape(p)}</text>')
    svg.append('</g></svg>')
    atomic_text(root/'survival-by-commit.svg','\n'.join(svg)+'\n')
