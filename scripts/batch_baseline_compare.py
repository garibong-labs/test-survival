#!/usr/bin/env python3
"""Disposable, no-model JSV comparison. Source experiments are read-only inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import yaml


def dump(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,indent=2)+'\n')


def setup(root, source):
    repo=root/'repo'
    if not repo.exists():
        subprocess.run(['git','clone','--no-hardlinks','--no-checkout',str(source),str(repo)],check=True)
        subprocess.run(['git','-C',str(repo),'checkout','--detach','0c14a7a8664334fd10108895f84d8232f6c34019'],check=True)
        subprocess.run(['git','-C',str(repo),'tag','batch-comparison-release','48ca8c3c4eb89e1d00ea0e8097daac187caff142'],check=True)
    inputs={'ouedraogo':source/'experiments/commit-survival','ktester':source/'experiments/commit-survival-run2'}
    classes=[]; hashes={}
    for group,exp in inputs.items():
        for c in json.loads((exp/'population/scope.json').read_text())['classes']:
            if c['class'] not in classes:classes.append(c['class'])
        src=exp/'generated'/group; dest=root/'generated-input/generated'/group
        if dest.exists():raise RuntimeError('Refusing to overwrite copied input '+str(dest))
        shutil.copytree(src,dest)
        hashes[group]={'source':str(src),'files':{str(p.relative_to(src)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(src.rglob('*')) if p.is_file()}}
    dump(root/'source-hashes.json',hashes)
    for mode in ('legacy','batch'):
        exp=repo/'experiments'/mode
        if exp.exists():raise RuntimeError('Refusing to overwrite experiment '+str(exp))
        cfg=dict(name='batch-baseline-comparison-'+mode,project=dict(repo=str(repo),language='java',build='maven',module='.',test_framework='junit5'),
                 anchor='0c14a7a8664334fd10108895f84d8232f6c34019',
                 range=dict(branch='origin/master',end='51fd82b420c3abde014f180ea109155de0d36480',mode='first-parent',limit=20,skip_untouched='main-sources'),
                 scope=dict(rule='explicit',classes=classes),dev=dict(stable_runs=1,groups=[dict(name='dev',denominator='scope-targeted')]),
                 model=dict(provider='openrouter',name='openai/gpt-oss-120b',env_file=None,env_key='BATCH_COMPARISON_NO_MODEL_KEY'),
                 generators=[dict(name='ouedraogo',method='ouedraogo',impl='native',params=dict(prompts=['ZSL','FSL','CoT','ToT','GToT']),rounds=dict(max=1,until_dev_matched=False)),
                             dict(name='ktester',method='ktester',impl='author-tool',tool_path=str(source.parent/'KTester'),rounds=dict(max=1,until_dev_matched=False))],
                 screen=dict(stable_runs=1,dedup='exact-body',matching='none'),
                 survival=dict(flaky_rerun=0,environment_errors=dict(patterns=[],max_reruns=0,backoff_seconds=0)),
                 coverage=dict(changed_line=True,at_anchor=True,groups='alive-entering'),
                 runner=dict(docker=dict(image='survival-java:25',m2_volume='batch-baseline-'+mode+'-m2'),reuse=dict(checkouts=None,classpath_cache=None),
                             workers=dict(build=1,survive=1,cover=1),timeouts=dict(build=600,compile=300,test_class=120),jvm=dict(launcher='1g',javac='512m',jacoco='1g')))
        if mode=='batch':cfg['survival']['execution']='batch'
        exp.mkdir(parents=True);(exp/'experiment.yaml').write_text(yaml.safe_dump(cfg,sort_keys=False))


def run(root, mode, tool):
    exp=root/'repo/experiments'/mode
    env={**os.environ,'PYTHONPATH':str(tool.resolve()/'src')}
    env.pop('BATCH_COMPARISON_NO_MODEL_KEY',None);env.pop('SURVIVAL_JVM_JACOCO',None)
    command=[sys.executable,'-m','test_survival.cli']
    steps=[('prepare-scope',['run',str(exp),'--until','scope','--no-panel']),
           ('import',['import',str(exp),'--from',str(root/'generated-input'),'--what','generated'])]
    steps += [(s,['run',str(exp),'--stage',s,'--no-panel']) for s in ('screen','control','survive','cover','report')]
    start=time.monotonic(); data=dict(mode=mode,tool_head=subprocess.check_output(['git','-C',str(tool),'rev-parse','HEAD'],text=True).strip(),steps=[])
    for name,args in steps:
        log=root/f'{mode}-{name}.log'; t=time.monotonic()
        print(mode,name,flush=True)
        with log.open('w') as stream:
            child=subprocess.Popen(command+args,env=env,stdout=stream,stderr=subprocess.STDOUT)
            try:code=child.wait(timeout=max(1,3600-(time.monotonic()-start)))
            except subprocess.TimeoutExpired:
                subprocess.run(command+['stop',str(exp)],env=env,check=False)
                child.wait(timeout=30);code=124
        data['steps'].append(dict(stage=name,seconds=round(time.monotonic()-t,3),exit_code=code,log=str(log)))
        data['seconds']=round(time.monotonic()-start,3)
        dump(root/f'{mode}-timing.json',data)
        if code:raise SystemExit(f'{mode}/{name} failed ({code}); see {log}')
    print(json.dumps(data),flush=True)


def compare(root):
    from test_survival.io import read_json, read_csv, result_exists
    from test_survival.config import load, group_names
    exps={mode:root/'repo/experiments'/mode for mode in ('legacy','batch')}
    configs={m:load(e) for m,e in exps.items()}
    groups=group_names(configs['legacy'])
    frozen={m:read_json(e/'results/frozen-denominators.json') for m,e in exps.items()}
    assert frozen['legacy']==frozen['batch'], 'Frozen populations differ'
    for g,tests in frozen['legacy'].items():
        for t in tests:
            assert (exps['legacy']/t['file']).read_bytes()==(exps['batch']/t['file']).read_bytes(), t['file']
    curves={m:read_csv(e/'results/survival.csv') for m,e in exps.items()}
    assert [r['sha'] for r in curves['legacy']]==[r['sha'] for r in curves['batch']]
    scope={c['file'] for c in read_json(exps['batch']/'population/scope.json')['classes']}
    normalized=lambda state:'fail' if state in ('error','timeout','fail') else state
    states={m:{g:{t['id']:'pass' for t in frozen[m][g]} for g in groups} for m in exps}
    differences=[]; method_cells=0
    for index,(left,right) in enumerate(zip(curves['legacy'],curves['batch'])):
        assert left['evaluated']==right['evaluated'], ('eligibility',index)
        for mode,e in exps.items():
            if index==0 or curves[mode][index]['evaluated']!='yes':continue
            for group in groups:
                raw=read_json(e/f'results/raw/{index}-{group}.json')
                for t in raw['methods']:
                    if states[mode][group][t['id']]=='pass':states[mode][group][t['id']]=normalized(t['state'])
        for group in groups:
            for ident,value in states['legacy'][group].items():
                method_cells+=1
                if value!=states['batch'][group][ident]:
                    differences.append(dict(index=index,group=group,id=ident,legacy=value,batch=states['batch'][group][ident]))
    coverage_diffs=[]; universe_diffs=[]; coverage_cells=0
    indices=[int(r['index']) for r in read_csv(exps['legacy']/'results/coverage-commits.csv')]
    assert indices==[int(r['index']) for r in read_csv(exps['batch']/'results/coverage-commits.csv')]
    assert read_json(exps['legacy']/'results/coverage-index-aliases.json')==read_json(exps['batch']/'results/coverage-index-aliases.json')
    for i in indices:
        for g in groups:
            maps={}
            for mode,e in exps.items():
                path=e/f'results/coverage/{i}-{g}-lines.csv'
                maps[mode]={(r['file'],int(r['line'])):r['covered'] for r in read_csv(path) if r['file'] in scope} if result_exists(path) else None
            if maps['legacy'] is None or maps['batch'] is None:
                if maps['legacy']!=maps['batch']:universe_diffs.append(dict(index=i,group=g,reason='availability differs'))
                continue
            if maps['legacy'].keys()!=maps['batch'].keys():
                universe_diffs.append(dict(index=i,group=g,legacy_only=sorted(maps['legacy'].keys()-maps['batch'].keys()),batch_only=sorted(maps['batch'].keys()-maps['legacy'].keys())))
            for (f,n) in sorted(maps['legacy'].keys()|maps['batch'].keys()):
                coverage_cells+=1
                a=maps['legacy'].get((f,n));b=maps['batch'].get((f,n))
                if a!=b:coverage_diffs.append(dict(index=i,group=g,file=f,line=n,legacy=a,batch=b))
    changed={m:read_csv(e/'results/changed-lines.csv') for m,e in exps.items()}
    assert len(changed['legacy'])==len(changed['batch'])
    assert not list((exps['batch']/'results/coverage').glob('*.xml*'))
    changed_diffs=[]
    for a,b in zip(changed['legacy'],changed['batch']):
        assert a['index']==b['index']
        for k in a:
            if k.startswith('scope_') and a[k]!=b[k]:changed_diffs.append(dict(index=int(a['index']),column=k,legacy=a[k],batch=b[k]))
        assert all(v=='' for k,v in b.items() if k.startswith(('all_','other_')))
    inputs=read_json(root/'source-hashes.json')
    for item in inputs.values():
        for name,digest in item['files'].items():
            assert hashlib.sha256((Path(item['source'])/name).read_bytes()).hexdigest()==digest, 'Finished source changed'
    result=dict(indices=len(curves['legacy'])-1,groups={g:len(ts) for g,ts in frozen['legacy'].items()},
                compared_method_cells=method_cells,survival_differences=differences,compared_scope_line_cells=coverage_cells,
                scope_universe_differences=universe_diffs,scope_line_differences=coverage_diffs,changed_scope_differences=changed_diffs,
                source_files_unchanged=True,timings={m:read_json(root/f'{m}-timing.json') for m in exps})
    dump(root/'comparison.json',result)
    print(json.dumps({k:(len(v) if k.endswith('differences') else v) for k,v in result.items() if k!='timings'},indent=2))


def policies(root, tool):
    """Exercise developer pools only; no generator or model calls."""
    exp=root/'repo/experiments/policies'
    cfg=yaml.safe_load((root/'repo/experiments/batch/experiment.yaml').read_text())
    cfg['name']='batch-baseline-comparison-policies'
    cfg['policies']=dict(baseline='experiments/batch',regenerate_developer=False,
        groups=['dev'],releases=['batch-comparison-release'],threshold=.5,
        max_threshold_triggers=1,concurrency={'dev_selection':1},set=[
            dict(name='release-reset',trigger='release',action='reset',developer='regenerate'),
            dict(name='half-reset',trigger='threshold',action='reset',developer='regenerate'),
            dict(name='half-fill',trigger='threshold',action='fill',developer='keep')])
    contents=yaml.safe_dump(cfg,sort_keys=False)
    path=exp/'experiment.yaml'
    if path.exists() and path.read_text()!=contents:
        raise RuntimeError('Refusing to overwrite policy configuration')
    exp.mkdir(parents=True,exist_ok=True);path.write_text(contents)
    env={**os.environ,'PYTHONPATH':str(tool.resolve()/'src')}
    env.pop('BATCH_COMPARISON_NO_MODEL_KEY',None)
    steps=[]
    for label,args in [('plan',['policy','plan',str(exp)]),('pools',['policy','run',str(exp),'--until','pools'])]:
        start=time.monotonic()
        with (root/f'policy-{label}.log').open('w') as stream:
            subprocess.run([sys.executable,'-m','test_survival.cli',*args],env=env,
                           stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=1800)
        steps.append(dict(stage=label,seconds=round(time.monotonic()-start,3)))
    plan=json.loads((exp/'results/policy/plan.json').read_text())
    funnels=[]
    for point in plan['pools']:
        assert point['group']=='dev'
        folder=exp/f'results/policy/pools/dev/{point["index"]}'
        funnel=json.loads((folder/'funnel.json').read_text())
        assert funnel['stable_runs']==cfg['dev']['stable_runs']
        assert (folder/'pool.json').exists()
        funnels.append(dict(index=point['index'],**funnel))
    assert funnels, 'Expected at least the release pool'
    dump(root/'policy-verification.json',dict(steps=steps,funnels=funnels,plan=plan,
         tool_head=subprocess.check_output(['git','-C',str(tool),'rev-parse','HEAD'],text=True).strip()))
    print(root/'policy-verification.json')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['setup','legacy','batch','compare','policies']);p.add_argument('--root',type=Path,default=Path('/tmp/batch-baseline-comparison'));p.add_argument('--tool',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--source',type=Path,default=Path.home()/'_projects/experiment-projects/json-schema-validator');a=p.parse_args()
    a.root.mkdir(parents=True,exist_ok=True)
    if a.action=='setup':setup(a.root,a.source)
    elif a.action=='compare':compare(a.root)
    elif a.action=='policies':policies(a.root,a.tool)
    else:run(a.root,a.action,a.tool)
