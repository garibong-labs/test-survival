from concurrent.futures import ThreadPoolExecutor, as_completed

from ..adapters.java_maven import JavaMavenAdapter
from ..config import group_names, derived_source
from ..io import read_json,read_csv,write_json,write_csv
from ..pipeline import log
from ..runner import measurement_session
from ..rules.survival import advance
from ..rules.commits import skip_reason
from ..rules.dev_denominator import derive_record,validate_derived_subsets


def skipped_commits(exp,config,commits):
    paths=read_json(exp/'results/commit-paths.json')
    return {int(row['index']):reason for row in commits if (reason:=skip_reason(paths[str(row['index'])],config['project']['module'],config['range']['skip_untouched']))}


def raw_folder(group,config,provenance=None):
    imported = (provenance or {}).get('groups', {}).get(group)
    if imported:
        if imported.get('raw_folder'):
            return imported['raw_folder']
        folders = {p.split('/')[1] for p in imported['files'] if p.startswith(('results/raw/', 'results/raw-derived/'))}
        if len(folders) == 1:
            return folders.pop()
        if len(folders) > 1:
            raise RuntimeError('Ambiguous imported survival paths for '+group+'; reimport developer results')
    return 'raw-derived' if derived_source(config, group) else 'raw'


def aggregate(exp,config):
    from ..dev_results import load_provenance
    provenance = load_provenance(exp)
    commits=read_csv(exp/'results/commits.csv');anchor=read_json(exp/'results/anchor.json')
    groups=group_names(config);skipped=skipped_commits(exp,config,commits)
    frozen=read_json(exp/'results/frozen-denominators.json')
    alive={g:{r['id'] for r in frozen[g]} for g in groups}
    zero=dict(index='0',sha=anchor['sha'],date=anchor['date'],subject='t = '+config['anchor'])
    curves=[];failures=[];entering={};missing=[];failed=[]
    for commit in [zero,*commits]:
        i=int(commit['index']);entering[str(i)]={g:sorted(ids) for g,ids in alive.items()}
        build_ok=i==0 or i in skipped or read_json(exp/f'results/builds/{i}.json')['build']=='ok'
        ok=build_ok and i not in skipped
        if not build_ok:failed.append(i)
        row={k:commit[k] for k in ['index','sha','date','subject']};row['evaluated']='yes' if ok else 'no'
        for g in groups:
            lost=[]
            if i and build_ok:
                raw=read_json(exp/f'results/{raw_folder(g,config,provenance)}/{i}-{g}.json')
                if raw is None:
                    missing.append(dict(index=i,group=g));continue
                if i in skipped:
                    if raw.get('status')!='skipped-untouched':raise RuntimeError('Skipped commit has a measured record')
                else:
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
    counts={folder:sum((exp/f'results/{folder}/{r["index"]}-{g}.json').exists() for g in groups if raw_folder(g,config,provenance)==folder for r in commits if int(r['index']) not in failed) for folder in ['raw','raw-derived']}
    n=len(commits)-len(failed)
    expected=sum(raw_folder(g,config,provenance)=='raw' for g in groups)*n
    derived_expected=sum(raw_folder(g,config,provenance)=='raw-derived' for g in groups)*n
    consistency=dict(nonincreasing=all(all(a[g+'_alive']>=b[g+'_alive'] for a,b in zip(curves,curves[1:])) for g in groups),index_zero_equals_denominators=all(curves[0][g+'_alive']==len(frozen[g]) for g in groups),raw_records=counts['raw'],expected_raw_records=expected,raw_count_matches=counts['raw']==expected and counts['raw-derived']==derived_expected,missing_records=[],build_fail_indices=failed,
                     measured_raw_records=counts['raw'],derived_raw_records=counts['raw-derived'],expected_measured_raw_records=expected,expected_derived_raw_records=derived_expected,skipped_indices=sorted(skipped))
    write_json(exp/'results/survival-consistency.json',consistency)
    return curves


def run(exp,config):
    from ..dev_results import imported_for, imported_record
    provenance, imported = imported_for(exp, config, 'survive')
    adapter=JavaMavenAdapter(exp,config)
    frozen=read_json(exp/'results/frozen-denominators.json');groups=group_names(config)
    validate_derived_subsets(config['dev']['groups'],frozen)
    commits=read_csv(exp/'results/commits.csv');skipped=skipped_commits(exp,config,commits)
    write_csv(exp/'results/skipped-commits.csv',[dict(index=r['index'],sha=r['sha'],reason=skipped[int(r['index'])]) for r in commits if int(r['index']) in skipped],['index','sha','reason'])
    for row in commits:
        if int(row['index']) not in skipped:continue
        for group in groups:
            if group in imported:
                imported_record(exp, f'results/{raw_folder(group,config,provenance)}/{row["index"]}-{group}.json', sha=row['sha'], provenance=provenance)
                continue
            write_json(exp/f'results/{raw_folder(group,config,provenance)}/{row["index"]}-{group}.json',dict(index=int(row['index']),sha=row['sha'],group=group,phase='A',status='skipped-untouched'))
    evaluated=[r for r in commits if int(r['index']) not in skipped and read_json(exp/f'results/builds/{r["index"]}.json')['build']=='ok']
    for row in evaluated:
        for g in imported:
            imported_record(exp, f'results/{raw_folder(g,config,provenance)}/{row["index"]}-{g}.json', sha=row['sha'], method_ids=[m['id'] for m in frozen[g]], provenance=provenance)
    measured=[g for g in groups if raw_folder(g,config,provenance)=='raw' and g not in imported]
    def job(g):
        alive={m['id'] for m in frozen[g]};dead_at={}
        # Parallelize groups, never dependent indices within a group. Cached
        # earlier records reconstruct entry sets before any later dispatch.
        for row in evaluated:
            i=int(row['index']);record=adapter.run_group(i,g,frozen[g],dead_at=dead_at)
            if any(run.get('exit_code') in [-9,137] for f in record['files'] for run in [f['compile'],*f['runs']]):
                source=exp/f'results/raw/{i}-{g}.json';dest=exp/f'results/quarantine/{i}-{g}.json';dest.parent.mkdir(parents=True,exist_ok=True);source.replace(dest)
                raise RuntimeError('Worker killed; quarantined raw record')
            if {m['id'] for m in record['methods']} != {m['id'] for m in frozen[g]}:
                raise RuntimeError('Raw methods differ from the frozen denominator')
            alive,lost=advance(alive,record['methods'])
            dead_at.update({m['id']:i for m in lost})
            log(exp,f'survive: {i} {g} recorded',config)
    with measurement_session(adapter, 'survive', bool(measured and evaluated)), ThreadPoolExecutor(max_workers=config['runner']['workers']['survive']) as pool:
        jobs=[pool.submit(job,g) for g in measured]
        for future in as_completed(jobs):future.result()
    pending={g['name']:g['derive_from'] for g in config['dev']['groups'] if g['name'] in groups and derived_source(config, g['name']) and g['name'] not in imported}
    while pending:
        ready=[g for g,source in pending.items() if source not in pending]
        if not ready:raise RuntimeError('Derived group cycle')
        for g in ready:
            source=pending.pop(g)
            for row in evaluated:
                raw=read_json(exp/f'results/{raw_folder(source,config,provenance)}/{row["index"]}-{source}.json')
                write_json(exp/f'results/raw-derived/{row["index"]}-{g}.json',derive_record(raw,g,frozen[g]))
    curves=aggregate(exp,config)
    return 'Final survivors: '+', '.join(f'{g}={curves[-1][g+"_alive"]}' for g in groups)
