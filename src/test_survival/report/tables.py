"""Report tables retain the reference's ordering, punctuation, and rounding."""
from collections import Counter
from ..io import read_csv,read_json,atomic_text,write_json


def md(value):return str(value).replace('|','\\|').replace('\n',' ')
def pct(n,d):return f'{n} ({100*n/d:.1f}%)' if d else f'{n} (n/a)'


def build(exp,config):
    groups=['dev',*[g['name'] for g in config['generators']]]
    labels={'dev':'Dev',**{g['name']:g['display'] for g in config['generators']}}
    counts=read_json(exp/'results/denominators.json')
    curves=read_csv(exp/'results/survival.csv');losses=read_csv(exp/'results/first-failures.csv')
    last=curves[-1]
    events=[r for r in curves if r['index']=='0' or any(int(r[g+'_lost']) for g in groups) or r['index']==last['index']]
    lines=['# Table 1. Survival by event','','| Index | Date | Subject | '+' | '.join(labels[g] for g in groups)+' |','|---:|---|---|'+'---:|'*len(groups)]
    for r in events:lines.append('| '+' | '.join([r['index'],r['date'][:10],md(r['subject']),*[pct(int(r[g+'_alive']),counts[g]) for g in groups]])+' |')
    atomic_text(exp/'results/table1-survival-by-event.md','\n'.join(lines)+'\n')
    lines=['# Table 2. Breaking commits','','Cells show methods lost and first-failure kinds.','','| Index | '+' | '.join(labels[g] for g in groups)+' |','|---:|'+'---|'*len(groups)]
    for index in sorted({int(r['index']) for r in losses}):
        cells=[]
        for g in groups:
            subset=[r for r in losses if int(r['index'])==index and r['group']==g]
            kinds=Counter(r['kind'] for r in subset)
            cells.append(str(len(subset))+(' ('+', '.join(f'{k}: {v}' for k,v in sorted(kinds.items()))+')' if subset else ''))
        lines.append('| '+str(index)+' | '+' | '.join(cells)+' |')
    atomic_text(exp/'results/table2-breaking-commits.md','\n'.join(lines)+'\n')
    rows=read_csv(exp/'results/changed-lines.csv')
    for row in rows:
        for key in row:
            if key.endswith(('_covered','_executable','_lost')) and row[key]!='':row[key]=int(row[key])
    coverage=read_json(exp/'results/coverage-consistency.json')
    lines=['# Table 3. Changed-line coverage','','Coverage uses methods alive entering each index. Changed-line coordinates are the post-image added/modified lines. Build failures and missing coverage remain unavailable.','']
    totals={}
    for category in ['all','scope','other']:
        lines += ['## '+category.capitalize()+' files','','| Index | Subject | Executable lines | '+' | '.join(labels[g]+' covered' for g in groups)+' | '+' | '.join(labels[g]+' lost' for g in groups)+' |','|---:|---|---:|'+'---:|'*(2*len(groups))]
        visible=[r for r in rows if r[category+'_executable'] not in ['',0]]
        for row in visible:
            n=row[category+'_executable'];cells=[row['index'],md(row['subject']),str(n)]
            cells += [pct(row[category+'_'+g+'_covered'],n) if row[category+'_'+g+'_covered']!='' else 'unavailable' for g in groups]
            cells += [str(row[g+'_lost']) for g in groups]
            lines.append('| '+' | '.join(cells)+' |')
        common=[r for r in visible if all(r[category+'_'+g+'_covered']!='' for g in groups)]
        n=sum(r[category+'_executable'] for r in common)
        covered={g:sum(r[category+'_'+g+'_covered'] for r in common) for g in groups}
        lost={g:sum(r[g+'_lost'] for r in common) for g in groups}
        totals[category]=dict(executable=n,**covered,methods_lost_in_listed_commits=lost)
        lines.append('| Total | Common measured commits | '+str(n)+' | '+' | '.join(pct(covered[g],n) for g in groups)+' | '+' | '.join(str(lost[g]) for g in groups)+' |')
        lines.append('')
    lines.append(f'Production-change commits: {len(rows)}. Commits with zero executable changed lines: {coverage["zero_executable_change_commits"]}. Commits without an executable-line measurement: {coverage["unavailable_commits"]}.')
    atomic_text(exp/'results/table3-changed-line-coverage.md','\n'.join(lines)+'\n')
    lines=['# Table 4. Exposure versus break','','Exposure is coverage at c−1 of at least one executable pre-image changed line. Break is any cumulative first failure at c. Pure insertions are unexposed.','','| Exposure / break | '+' | '.join(labels[g] for g in groups)+' |','|---|'+'---:|'*len(groups)]
    exposure={g:Counter() for g in groups}
    for row in rows:
        for g in groups:
            pre=row[g+'_pre_image_covered'];post=row['all_'+g+'_covered']
            if pre=='' or post=='':exposure[g]['unavailable']+=1
            else:exposure[g][(pre>0,row[g+'_lost']>0)]+=1
    for label,key in [('Exposed / lost',(True,True)),('Exposed / no loss',(True,False)),('Unexposed / lost',(False,True)),('Unexposed / no loss',(False,False)),('Unavailable','unavailable')]:
        lines.append('| '+label+' | '+' | '.join(str(exposure[g][key]) for g in groups)+' |')
    atomic_text(exp/'results/table4-exposure-vs-break.md','\n'.join(lines)+'\n')
    lines=['# Coverage at t','','| Scope file | Executable lines | '+' | '.join(labels[g] for g in groups)+' |','|---|---:|'+'---:|'*len(groups)]
    scope={r['file'] for r in read_json(exp/'population/scope.json')['classes']}
    maps={}
    for g in groups:
        path=exp/f'results/coverage/0-{g}-lines.csv'
        maps[g]={(r['file'],int(r['line'])):r['covered']=='yes' for r in read_csv(path)} if path.exists() else None
    available=[m for m in maps.values() if m is not None];baseline={}
    for file in sorted(scope):
        keys={p for p in available[0] if p[0]==file} if available else set()
        covered={g:sum(maps[g][p] for p in keys) if maps[g] is not None else None for g in groups}
        baseline[file]=dict(executable=len(keys),**covered)
        lines.append('| '+file+' | '+str(len(keys))+' | '+' | '.join(pct(covered[g],len(keys)) if covered[g] is not None else 'unavailable' for g in groups)+' |')
    atomic_text(exp/'results/coverage-t.md','\n'.join(lines)+'\n')
    coverage.update(totals=totals,baseline=baseline);write_json(exp/'results/coverage-consistency.json',coverage)
