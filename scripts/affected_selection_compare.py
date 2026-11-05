#!/usr/bin/env python3
"""Disposable affected/all verification; original experiments are read-only inputs."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import yaml
import batch_baseline_compare as baseline


def dump(path,value): baseline.dump(path,value)


def setup(root, project):
    source=Path.home()/'_projects/experiment-projects'/project
    if project=='json-schema-validator':
        baseline.setup(root,source)
        original=yaml.safe_load((root/'repo/experiments/batch/experiment.yaml').read_text())
        for old,new in [('legacy','all'),('batch','affected')]:
            (root/'repo/experiments'/old).rename(root/'repo/experiments'/new)
    else:
        repo=root/'repo'
        subprocess.run(['git','clone','--no-hardlinks','--no-checkout',str(source),str(repo)],check=True)
        source_exp=source/'experiments/commit-survival'
        original=yaml.safe_load((source_exp/'experiment.yaml').read_text())
        subprocess.run(['git','-C',str(repo),'checkout','--detach',original['anchor']],check=True)
        original['project']['repo']=str(repo)
        original['range']['limit']=60
        # Keep dev as a selection-only parent; only dev-scope is frozen/measured.
        original['generators']=[g for g in original['generators'] if g['name']=='ouedraogo']
        original['model']=dict(provider='openrouter',name=original['model']['name'],env_file=None,env_key='AFFECTED_NO_MODEL_KEY')
        original['runner']['docker'].pop('mounts',None);original['runner']['docker'].pop('env_file',None)
        original['runner']['workers']={'build':1,'survive':1,'cover':1}
        original['survival']['flaky_rerun']=0
        original['survival']['environment_errors']={'patterns':[],'max_reruns':0,'backoff_seconds':0}
        src=source_exp/'generated/ouedraogo';target=root/'generated-input/generated/ouedraogo'
        shutil.copytree(src,target)
        dump(root/'source-hashes.json',{'ouedraogo':{'source':str(src),'files':{str(p.relative_to(src)):hashlib.sha256(p.read_bytes()).hexdigest() for p in src.rglob('*') if p.is_file()}}})
    for mode in ('all','affected'):
        config=deepcopy(original);config['name']='affected-'+project+'-'+mode
        config['survival'].update(execution='batch',selection=mode)
        config['runner']['docker']['m2_volume']='affected-'+project+'-'+mode+'-m2'
        exp=root/'repo/experiments'/mode;exp.mkdir(parents=True,exist_ok=True)
        path=exp/'experiment.yaml'
        path.write_text(yaml.safe_dump(config,sort_keys=False))
    dump(root/'setup.json',dict(project=project,original_source=str(source),created=time.time()))


def run(root,mode,tool,policy=False):
    exp=root/'repo/experiments'/((mode+'-policies') if policy else mode)
    env={**os.environ,'PYTHONPATH':str(tool.resolve()/'src')}
    env.pop('AFFECTED_NO_MODEL_KEY',None);env.pop('BATCH_COMPARISON_NO_MODEL_KEY',None);env.pop('SURVIVAL_JVM_JACOCO',None)
    if policy:
        config=yaml.safe_load((root/'repo/experiments'/mode/'experiment.yaml').read_text())
        config['name']+='-policies'
        config['policies']=dict(baseline='experiments/'+mode,groups=['dev'],regenerate_developer=True,
            releases=['batch-comparison-release'],threshold=.5,max_threshold_triggers=1,concurrency={'dev_selection':1},set=[
            dict(name='reset',trigger='release',action='reset',developer='regenerate'),
            dict(name='fill',trigger='threshold',action='fill',developer='regenerate')])
        exp.mkdir(parents=True,exist_ok=True);(exp/'experiment.yaml').write_text(yaml.safe_dump(config,sort_keys=False))
        steps=[('plan',['policy','plan',str(exp)]),('run',['policy','run',str(exp)])]
    else:
        steps=[('prepare-scope',['run',str(exp),'--until','scope','--no-panel']),
               ('import',['import',str(exp),'--from',str(root/'generated-input'),'--what','generated'])]
        steps += [(s,['run',str(exp),'--stage',s,'--no-panel']) for s in ('screen','control','survive','cover','report')]
    label=mode+('-policies' if policy else '')
    start=time.monotonic();data=dict(mode=mode,policy=policy,tool_head=subprocess.check_output(['git','-C',str(tool),'rev-parse','HEAD'],text=True).strip(),steps=[])
    for name,args in steps:
        log=root/f'{label}-{name}.log';t=time.monotonic()
        print(label,name,flush=True)
        with log.open('w') as output:
            child=subprocess.Popen([sys.executable,'-m','test_survival.cli',*args],env=env,stdout=output,stderr=subprocess.STDOUT)
            try: code=child.wait(timeout=6*3600)
            except subprocess.TimeoutExpired:
                subprocess.run([sys.executable,'-m','test_survival.cli','stop',str(exp)],env=env,check=False)
                child.wait(timeout=60);code=124
        data['steps'].append(dict(stage=name,seconds=round(time.monotonic()-t,3),exit_code=code,log=str(log)))
        data['seconds']=round(time.monotonic()-start,3);dump(root/f'{label}-timing.json',data)
        if code: raise SystemExit(f'{label}/{name} failed ({code}); see {log}')


def compare(root,policy=False):
    from test_survival.io import read_json,read_csv,logical_glob,result_exists
    exps={m:root/'repo/experiments'/(m+'-policies' if policy else m) for m in ('all','affected')}
    differences=[]
    def diff(kind,identity,left,right):
        if left!=right: differences.append(dict(kind=kind,identity=identity,all=left,affected=right))
    if policy:
        for p in ('reset','fill'):
            for name in ('survival.csv','first-failures.csv','coverage-commits.csv'):
                values={m:read_csv(e/'results/policy'/p/name) for m,e in exps.items()}
                # IDs include equal source hashes and do not depend on output roots.
                diff('policy-table',[p,name],values['all'],values['affected'])
    else:
        frozen={m:read_json(e/'results/frozen-denominators.json') for m,e in exps.items()}
        from test_survival.config import load,execution_groups
        groups=execution_groups(load(exps['all']),frozen['all'])
        diff('frozen','all',frozen['all'],frozen['affected'])
        for g,ts in groups.items():
            for t in ts:
                diff('source',t['id'],hashlib.sha256((exps['all']/t['file']).read_bytes()).hexdigest(),hashlib.sha256((exps['affected']/t['file']).read_bytes()).hexdigest())
        curves={m:read_csv(e/'results/survival.csv') for m,e in exps.items()}
        diff('survival','table',curves['all'],curves['affected'])
        states={m:{g:{t['id']:'pass' for t in ts} for g,ts in groups.items()} for m in exps}
        for row in curves['all']:
            index=int(row['index'])
            if index and row['evaluated']=='yes':
                for m,e in exps.items():
                    for g in groups:
                        path=e/f'results/raw-derived/{index}-{g}.json'
                        if not result_exists(path):path=e/f'results/raw/{index}-{g}.json'
                        for t in read_json(path)['methods']:
                            if states[m][g][t['id']]=='pass':states[m][g][t['id']]=t['state']
            for g in groups:
                for ident,value in states['all'][g].items():diff('method',[index,g,ident],value,states['affected'][g][ident])
        for name in ('first-failures.csv','changed-lines.csv','coverage-commits.csv'):
            diff(name,'table',read_csv(exps['all']/'results'/name),read_csv(exps['affected']/'results'/name))
        for path in logical_glob(exps['all']/'results/coverage','*-lines.csv'):
            diff('scope-lines',path.name,read_csv(path),read_csv(exps['affected']/'results/coverage'/path.name))
    summary={}
    for m,e in exps.items():
        batches=e/('results/policy/batches' if policy else 'results/baseline-batches')
        runs=[r for path in logical_glob(batches,'*.json') if not path.name.endswith('-measurement.json') for r in read_json(path,{}).get('runs',[])]
        summary[m]=dict(timing=read_json(root/f'{m}{"-policies" if policy else ""}-timing.json'),
            memory={name:max((r.get('memory',{}).get(name,0) for r in runs),default=0) for name in ('peak_rss_bytes','heap_pool_peak_sum_bytes','nonheap_pool_peak_sum_bytes')},
            final=read_json(e/'results/final-summary.json',{}) if not policy else {p:read_json(e/'results/policy'/p/'final-summary.json',{}) for p in ('reset','fill')})
    for group,item in read_json(root/'source-hashes.json').items():
        for name,digest in item['files'].items():
            assert hashlib.sha256((Path(item['source'])/name).read_bytes()).hexdigest()==digest,'Original source changed'
    result=dict(differences=differences,summary=summary,source_files_unchanged=True)
    dump(root/('policy-comparison.json' if policy else 'comparison.json'),result)
    print(json.dumps(dict(differences=len(differences),memory={m:d['memory'] for m,d in summary.items()}),indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['setup','all','affected','compare','policy-all','policy-affected','policy-compare'])
    p.add_argument('--root',type=Path,required=True);p.add_argument('--project',choices=['json-schema-validator','dnsjava'],default='json-schema-validator')
    p.add_argument('--tool',type=Path,default=Path(__file__).resolve().parents[1]);a=p.parse_args();a.root.mkdir(parents=True,exist_ok=True)
    if a.action=='setup':setup(a.root,a.project)
    elif a.action.endswith('compare'):compare(a.root,a.action.startswith('policy-'))
    else:run(a.root,a.action.removeprefix('policy-'),a.tool,a.action.startswith('policy-'))
