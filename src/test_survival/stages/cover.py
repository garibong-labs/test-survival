from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor,as_completed
import re

from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json,read_csv,write_json,write_csv
from ..pipeline import log
from ..runner import measurement_session
from ..config import group_names
from .survive import skipped_commits


def changed_lines(diff, module='.'):
    pre,post=defaultdict(set),defaultdict(set)
    module=module.removeprefix('./').rstrip('/')
    prefix='' if module in ['', '.'] else module+'/'
    old=new=None
    for line in diff.splitlines():
        if line.startswith('--- '):old=line[4:].removeprefix('a/').removeprefix(prefix)
        elif line.startswith('+++ '):new=line[4:].removeprefix('b/').removeprefix(prefix)
        elif line.startswith('@@ '):
            match=re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@',line)
            if not match:raise ValueError('Malformed diff hunk')
            a,ac,b,bc=match.groups()
            if old!='/dev/null':pre[old].update(range(int(a),int(a)+int(ac if ac is not None else 1)))
            if new!='/dev/null':post[new].update(range(int(b),int(b)+int(bc if bc is not None else 1)))
    return {k:sorted(v) for k,v in pre.items()},{k:sorted(v) for k,v in post.items()}


def aggregate(exp,config):
    groups=group_names(config)
    commits=read_csv(exp/'results/commits.csv')
    curves={int(r['index']):r for r in read_csv(exp/'results/survival.csv')}
    scope={r['file'] for r in read_json(exp/'population/scope.json')['classes']}
    indices=[int(r['index']) for r in read_csv(exp/'results/coverage-commits.csv')]
    aliases=read_json(exp/'results/coverage-index-aliases.json',{})
    maps={};missing=[];identical=[];mismatches=[]
    for i in indices:
        maps[i]={}
        failed_build = i != 0 and read_json(exp/f'results/builds/{i}.json', {}).get('build') == 'fail'
        for g in groups:
            path=exp/f'results/coverage/{i}-{g}-lines.csv'
            if failed_build or not path.exists():maps[i][g]=None;missing.append(dict(index=i,group=g));continue
            rows=read_csv(path);data={(r['file'],int(r['line'])):r['covered']=='yes' for r in rows}
            if len(data)!=len(rows):raise RuntimeError('Duplicate coverage lines')
            maps[i][g]=data
        available=[set(m) for m in maps[i].values() if m is not None]
        if available and any(m!=available[0] for m in available):mismatches.append(i)
        elif len(available)==len(groups):identical.append(i)
    write_json(exp/'results/coverage-universe-mismatches.json',mismatches)
    if mismatches:raise RuntimeError('Executable line universes differ across groups')
    rows=[];zero=unavailable=0
    def flatten(image):return {(file,int(line)) for file,lines in image.items() for line in lines}
    for commit in commits:
        if commit['changes-src-main']!='yes' or not config['coverage']['changed_line']:continue
        i=int(commit['index']);change=read_json(exp/f'results/changes/{i}.json')
        post=flatten(change['post_image']);pre=flatten(change['pre_image'])
        current=maps[i];previous=maps[aliases.get(str(i-1),i-1)];available=[m for m in current.values() if m is not None]
        row={k:commit[k] for k in ['index','sha','date','subject']}
        for category,changed in [('all',post),('scope',{p for p in post if p[0] in scope}),('other',{p for p in post if p[0] not in scope})]:
            executable=changed&available[0].keys() if available else None
            n=len(executable) if executable is not None else ''
            row[category+'_executable']=n
            for g in groups:
                count=sum(current[g][p] for p in executable) if executable is not None and current[g] is not None else ''
                row[category+'_'+g+'_covered']=count
                row[category+'_'+g+'_percent']=round(100*count/n,3) if n not in ['',0] and count!='' else ''
        for g in groups:
            row[g+'_lost']=int(curves[i][g+'_lost'])
            before=previous[g]
            row[g+'_pre_image_covered']=sum(before[p] for p in pre&before.keys()) if before is not None else ''
        if row['all_executable']=='':unavailable+=1
        elif row['all_executable']==0:zero+=1
        rows.append(row)
    write_csv(exp/'results/changed-lines.csv',rows,list(rows[0]) if rows else ['index'])
    result=dict(coverage_indices=len(indices),production_changes=len(rows),identical_executable_line_sets=identical,missing=missing,zero_executable_change_commits=zero,unavailable_commits=unavailable)
    write_json(exp/'results/coverage-consistency.json',result)
    return result


def run(exp,config):
    from ..dev_results import load_provenance, imported_groups, imported_record, require_record
    provenance = load_provenance(exp)
    imported = imported_groups(exp, config, 'cover', provenance=provenance)
    adapter=JavaMavenAdapter(exp,config)
    commits=read_csv(exp/'results/commits.csv');indices={0} if config['coverage']['at_anchor'] else set()
    skipped=skipped_commits(exp,config,commits);aliases={}
    def previous_evaluated(index):
        original=index
        while index in skipped:index-=1
        if original!=index:aliases[str(original)]=index
        return index
    if config['coverage']['changed_line']:
        for row in commits:
            if row['changes-src-main']!='yes':continue
            i=int(row['index']);indices.update([i,previous_evaluated(i-1)]);sha=row['sha'];parent=adapter.git('rev-parse',sha+'^1')
            module=config['project']['module'].removeprefix('./').rstrip('/')
            prefix=module+'/' if module not in ['', '.'] else ''
            diff=adapter.git('diff','--no-ext-diff','--find-renames','-U0',parent,sha,'--',prefix+'src/main/java')
            pre,post=changed_lines(diff,module)
            write_json(exp/f'results/changes/{i}.json',dict(index=i,sha=sha,parent=parent,pre_image=pre,post_image=post,diff=diff))
    indices-=set(skipped)
    write_json(exp/'results/coverage-index-aliases.json',aliases)
    write_csv(exp/'results/coverage-commits.csv',[dict(index=i) for i in sorted(indices)],['index'])
    alive=read_json(exp/'results/alive-entering.json');frozen=read_json(exp/'results/frozen-denominators.json')
    def job(i,g):
        selected=set(alive[str(i)][g]);methods=[r for r in frozen[g] if r['id'] in selected]
        raw=adapter.coverage(i,[],methods,group=g)
        if raw['status']!='evaluated':raise RuntimeError('Coverage report failed')
        log(exp,f'cover: {i} {g} recorded',config)
    shas = {int(r['index']): r['sha'] for r in commits}
    shas[0] = read_json(exp/'results/anchor.json')['sha']
    for i in sorted(indices):
        if i and read_json(exp/f'results/builds/{i}.json')['build'] != 'ok':
            continue
        for g in imported:
            raw = imported_record(exp, f'results/raw-coverage/{i}-{g}.json', sha=shas[i], method_ids=alive[str(i)][g], provenance=provenance)
            if raw.get('status') != 'evaluated':
                raise RuntimeError('Imported coverage report failed')
            require_record(exp, provenance, f'results/coverage/{i}-{g}-lines.csv')
    pending = [(i, g) for i in sorted(indices) for g in frozen if g not in imported
               and (i == 0 or read_json(exp/f'results/builds/{i}.json')['build'] == 'ok')]
    with measurement_session(adapter, 'cover', bool(pending)), ThreadPoolExecutor(max_workers=config['runner']['workers']['cover']) as pool:
        jobs = [pool.submit(job, i, g) for i, g in pending]
        for future in as_completed(jobs):future.result()
    # Restrict the stored changed-line images to executable coordinates.
    for row in commits:
        i=int(row['index']);path=exp/f'results/changes/{i}.json'
        if not path.exists():continue
        change=read_json(path)
        for key,image_index in [('pre_image',i-1),('post_image',i)]:
            image_index=aliases.get(str(image_index),image_index)
            first_dev=config['dev']['groups'][0]['name']
            universe_path=exp/f'results/coverage/{image_index}-{first_dev}-lines.csv'
            if universe_path.exists():
                universe={(r['file'],int(r['line'])) for r in read_csv(universe_path)}
                change[key]={f:[n for n in ns if (f,n) in universe] for f,ns in change[key].items()}
        write_json(path,change)
    result=aggregate(exp,config)
    return f'{result["coverage_indices"]} coverage indices; {result["production_changes"]} production changes'
