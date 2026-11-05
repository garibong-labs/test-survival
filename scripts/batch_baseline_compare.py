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


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['setup','legacy','batch']);p.add_argument('--root',type=Path,default=Path('/tmp/batch-baseline-comparison'));p.add_argument('--tool',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--source',type=Path,default=Path.home()/'_projects/experiment-projects/json-schema-validator');a=p.parse_args()
    a.root.mkdir(parents=True,exist_ok=True)
    if a.action=='setup':setup(a.root,a.source)
    else:run(a.root,a.action,a.tool)
