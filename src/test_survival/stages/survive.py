from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy

from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json,read_csv,write_json,write_csv
from ..pipeline import log
from ..rules.survival import advance,unchanged_module


def aggregate(exp,config):
    commits=read_csv(exp/'results/commits.csv');anchor=read_json(exp/'results/anchor.json')
    groups=['dev',*[g['name'] for g in config['generators']]]
    frozen=read_json(exp/'results/frozen-denominators.json')
    alive={g:{r['id'] for r in frozen[g]} for g in groups}
    zero=dict(index='0',sha=anchor['sha'],date=anchor['date'],subject='t = '+config['anchor'])
    curves=[];failures=[];entering={};missing=[];failed=[]
    for commit in [zero,*commits]:
        i=int(commit['index']);entering[str(i)]={g:sorted(ids) for g,ids in alive.items()}
        ok=i==0 or read_json(exp/f'results/builds/{i}.json')['build']=='ok'
        if not ok:failed.append(i)
        row={k:commit[k] for k in ['index','sha','date','subject']};row['evaluated']='yes' if ok else 'no'
        for g in groups:
            lost=[]
            if i and ok:
                raw=read_json(exp/f'results/raw/{i}-{g}.json')
                if raw is None:
                    missing.append(dict(index=i,group=g))
                    continue
                if {r['id'] for r in raw['methods']}!={r['id'] for r in frozen[g]}:
                    raise RuntimeError('Raw methods differ from the frozen denominator')
                alive[g],lost=advance(alive[g],raw['methods'])
                failures.extend(dict(index=i,sha=commit['sha'],group=g,id=m['id'],**{'class':m['class']},method=m['method'],kind=m['state'],flaky=m['flaky']) for m in lost)
            row[g+'_alive']=len(alive[g]);row[g+'_lost']=len(lost)
        curves.append(row)
    if missing:
        write_json(exp/'results/missing-survival-records.json',missing)
        raise RuntimeError('Missing survival records; resume survive stage')
    write_csv(exp/'results/survival.csv',curves,['index','sha','date','subject','evaluated']+[g+s for g in groups for s in ['_alive','_lost']])
    write_csv(exp/'results/first-failures.csv',failures,['index','sha','group','id','class','method','kind','flaky'])
    write_json(exp/'results/alive-entering.json',entering)
    actual=len(list((exp/'results/raw').glob('*.json')))
    expected=(len(commits)-len(failed))*len(groups)
    consistency=dict(nonincreasing=all(all(a[g+'_alive']>=b[g+'_alive'] for a,b in zip(curves,curves[1:])) for g in groups),index_zero_equals_denominators=all(curves[0][g+'_alive']==len(frozen[g]) for g in groups),raw_records=actual,expected_raw_records=expected,raw_count_matches=actual==expected,missing_records=missing,build_fail_indices=failed)
    write_json(exp/'results/survival-consistency.json',consistency)
    return curves


def run(exp,config):
    adapter=JavaMavenAdapter(exp,config);adapter.setup_runner()
    frozen=read_json(exp/'results/frozen-denominators.json')
    commits=read_csv(exp/'results/commits.csv');paths=read_json(exp/'results/commit-paths.json')
    evaluated=[r for r in commits if read_json(exp/f'results/builds/{r["index"]}.json')['build']=='ok']
    cache=config['survival']['dev_cache']=='unchanged-module'
    def job(row,g):
        i=int(row['index']);record=adapter.run_group(i,g,frozen[g])
        if any(run.get('exit_code') in [-9,137] for f in record['files'] for run in [f['compile'],*f['runs']]):
            # Do not freeze OOMs as test failures; retain evidence and retry on resume.
            source=exp/f'results/raw/{i}-{g}.json';dest=exp/f'results/quarantine/{i}-{g}.json';dest.parent.mkdir(parents=True,exist_ok=True);source.replace(dest)
            raise RuntimeError('Worker killed; quarantined raw record')
        log(exp,f'survive: {i} {g} recorded')
    with adapter.runner.session('survive'), ThreadPoolExecutor(max_workers=config['runner']['workers']['survive']) as pool:
        jobs=[pool.submit(job,row,g) for row in evaluated for g in frozen if not (cache and g=='dev')]
        if cache:
            previous=None
            for row in evaluated:
                i=int(row['index'])
                if previous is not None and unchanged_module(paths[str(i)],config['project']['module']):
                    record=deepcopy(read_json(exp/f'results/raw/{previous}-dev.json'))
                    record.update(index=i,sha=row['sha'],reused_from=previous)
                    write_json(exp/f'results/raw/{i}-dev.json',record)
                    log(exp,f'survive: {i} dev reused from {previous}')
                else:job(row,'dev')
                previous=i
        for future in as_completed(jobs):future.result()
    curves=aggregate(exp,config)
    return 'Final survivors: '+', '.join(f'{g}={curves[-1][g+"_alive"]}' for g in frozen)
