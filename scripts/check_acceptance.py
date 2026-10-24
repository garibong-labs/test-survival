"""Compare replay outputs with archived results without touching the reference."""
import argparse
import csv
import json
from pathlib import Path
import re

p=argparse.ArgumentParser();p.add_argument('replay',type=Path);p.add_argument('reference',type=Path);p.add_argument('--checkpoint',choices=['prepare','scope','screen','survive','cover','report'],required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
results=[]

def check(name,actual,expected):
    equal=actual==expected
    item={'file':name,'identical':equal}
    if not equal:
        left=str(actual).splitlines();right=str(expected).splitlines()
        for i in range(max(len(left),len(right))):
            x=left[i] if i<len(left) else '<EOF>';y=right[i] if i<len(right) else '<EOF>'
            if x!=y:
                item.update(first_differing_line=i+1,actual=x[:500],expected=y[:500]);break
    results.append(item)


def read(root,name):return json.loads((root/name).read_text())
def csvrows(root,name):
    with (root/name).open() as stream:return list(csv.DictReader(stream))

if a.checkpoint=='prepare':
    check('results/commits.csv SHA sequence',[r['sha'] for r in csvrows(a.replay,'results/commits.csv')],[r['sha'] for r in csvrows(a.reference,'results/commits.csv')])
    for key in ['sha','origin_master','n']:check('results/anchor.json '+key,read(a.replay,'results/anchor.json')[key],read(a.reference,'results/anchor.json')[key])
    builds=list((a.replay/'results/builds').glob('*.json'))
    results.append({'file':'results/builds/','records':len(builds),'failed':[p.name for p in builds if json.loads(p.read_text())['build']=='fail']})
elif a.checkpoint=='scope':
    for name in ['results/scope-selection.json']:
        check(name,read(a.replay,name),read(a.reference,name))
    check('population/scope.json classes',[r['class'] for r in read(a.replay,'population/scope.json')['classes']],[r['class'] for r in read(a.reference,'population/scope.json')['classes']])
    check('dev/dev-denominator.csv',sorted(csvrows(a.replay,'dev/dev-denominator.csv'),key=str),sorted(csvrows(a.reference,'dev/dev-denominator.csv'),key=str))
elif a.checkpoint=='screen':
    check('results/denominators.json',read(a.replay,'results/denominators.json'),read(a.reference,'results/denominators.json'))
    for g in read(a.replay,'results/denominators.json'):
        if g=='dev':continue
        name=f'results/llm-{g}-denominator.csv';check(name,sorted(csvrows(a.replay,name),key=str),sorted(csvrows(a.reference,name),key=str))
else:
    names={'survive':['survival.csv','first-failures.csv'],'cover':['changed-lines.csv','coverage-commits.csv'],'report':['table1-survival-by-event.md','table2-breaking-commits.md','table3-changed-line-coverage.md','table4-exposure-vs-break.md','coverage-t.md','survival.csv','first-failures.csv','changed-lines.csv','coverage-commits.csv','consistency.md']}[a.checkpoint]
    for name in names:
        name='results/'+name
        if name.endswith('first-failures.csv'):
            check(name,sorted(csvrows(a.replay,name),key=str),sorted(csvrows(a.reference,name),key=str))
        elif name.endswith('consistency.md'):
            def block(root):return json.loads(re.search(r'```json\n(.*?)\n```',(root/name).read_text(),re.S)[1])
            check(name+' JSON block',block(a.replay),block(a.reference))
        else:
            def normalize(root):return '\n'.join(line.rstrip() for line in (root/name).read_text().splitlines())
            check(name,normalize(a.replay),normalize(a.reference))
a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(json.dumps(results,indent=2,ensure_ascii=False)+'\n')
for row in results:print(row['file']+': '+('identical' if row.get('identical') else 'differs' if 'identical' in row else str(row)))
raise SystemExit(1 if any(r.get('identical') is False for r in results) else 0)
