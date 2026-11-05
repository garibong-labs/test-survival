#!/usr/bin/env python3
"""Real Docker fixture: batch baseline, developer pools, mixed policies and resume."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import yaml

import footprint_fixture


def main(root, selection='all'):
    root=root.resolve()
    footprint_fixture.ROOT=root;footprint_fixture.TARGET=root/'target';footprint_fixture.create()
    repo=root/'target';baseline=repo/'experiments/baseline';policy=repo/'experiments/policy'
    for exp in (baseline,policy):
        cfg=yaml.safe_load((exp/'experiment.yaml').read_text())
        cfg['project']['repo']=str(repo)
        cfg['dev']['stable_runs']=cfg['screen']['stable_runs']=1
        cfg['survival']={'execution':'batch','selection':selection}
        cfg['runner']['docker']['m2_volume']='footprint-e2e-m2'
        if exp==policy:
            cfg['policies'].update(regenerate_developer=False,threshold=.25,set=[
                dict(name='release-reset',trigger='release',action='reset',developer='regenerate'),
                dict(name='half-reset',trigger='threshold',action='reset',developer='regenerate'),
                dict(name='half-fill',trigger='threshold',action='fill',developer='keep')])
        (exp/'experiment.yaml').write_text(yaml.safe_dump(cfg,sort_keys=False))
    code=Path(__file__).resolve().parents[1]
    env={**os.environ,'PYTHONPATH':str(code/'src')};env.pop('OPENROUTER_API_KEY',None)
    command=[sys.executable,'-m','test_survival.cli'];timings=[]
    def call(name,args):
        start=time.monotonic()
        with (root/(name+'.log')).open('w') as stream:
            subprocess.run(command+list(map(str,args)),env=env,stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=600)
        timings.append(dict(step=name,seconds=round(time.monotonic()-start,3)))
    call('prepare',['run',baseline,'--until','scope','--no-panel'])
    call('import',['import',baseline,'--from',root/'generated-fixture','--what','generated'])
    for stage in ('screen','control','survive','cover','report'):
        call(stage,['run',baseline,'--stage',stage,'--no-panel'])
    call('policy-plan',['policy','plan',policy])
    call('policy-pools',['policy','run',policy,'--until','pools'])
    call('policy-run',['policy','run',policy])
    # Exercise the actual lifecycle stop during batch work in a separate fresh
    # experiment, retaining the uninterrupted fixture for an exact comparison.
    import shutil
    interrupted=repo/'experiments/interrupted'
    shutil.copytree(baseline,interrupted)
    for path in (interrupted/'state').glob('*.json'):
        if path.stem in ('control','survive','cover','report'):path.unlink()
    for name in ('baseline-batches','raw','raw-derived','raw-coverage','coverage','control'):
        shutil.rmtree(interrupted/'results'/name,ignore_errors=True)
    # Rebuild the removed successful workspaces through their guarded restoration path.
    call('interrupt-control',['run',interrupted,'--stage','control','--no-panel'])
    with (root/'interrupt-attempt.log').open('w') as stream:
        child=subprocess.Popen(command+['run',str(interrupted),'--stage','survive','--no-panel'],env=env,stdout=stream,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+120
        while not (interrupted/'work/baseline-batches/2/batches.tsv').exists():
            if child.poll() is not None or time.monotonic()>deadline:raise RuntimeError('No live batch to interrupt')
            time.sleep(.1)
        call('live-status',['status',interrupted,'--no-panel'])
        call('stop',['stop',interrupted]);child.wait(timeout=30)
        call('stopped-status',['status',interrupted,'--no-panel'])
    call('resume-survive',['run',interrupted,'--stage','survive','--no-panel'])
    call('resume-cover',['run',interrupted,'--stage','cover','--no-panel'])
    call('resumed-status',['status',interrupted,'--no-panel'])
    for name in ('survival.csv','first-failures.csv','changed-lines.csv'):
        assert (baseline/'results'/name).read_bytes()==(interrupted/'results'/name).read_bytes(),name
    sys.path.insert(0,str(code/'src'))
    from test_survival.io import read_json,read_csv
    assert read_json(interrupted/'state/survive.json')['status']=='completed'
    assert 'running' in (root/'live-status.log').read_text()
    assert 'stopped' in (root/'stopped-status.log').read_text()
    for name in ('release-reset','half-reset','half-fill'):
        segments=read_json(policy/f'results/policy/{name}/segments.json')['dev-scope']
        assert len(segments)==(1 if name=='half-fill' else 2)
        if name!='half-fill':assert segments[-1]['tests']
    pool=read_json(policy/'results/policy/pools/dev-scope/2/pool.json');assert pool
    funnel=read_json(policy/'results/policy/pools/dev-scope/2/funnel.json')
    assert funnel['stable_runs']==1 and funnel['stable_passed']>0
    assert list((policy/'results/policy/runs').glob('*/3.json'))
    assert not list((baseline/'results/coverage').glob('*.xml*'))
    assert all(r['all_executable']==r['other_executable']=='' for r in read_csv(baseline/'results/changed-lines.csv'))
    prior={p:p.read_bytes() for p in (policy/'results/policy/pools').rglob('pool.json')}
    call('policy-resume',['policy','run',policy])
    assert all(p.read_bytes()==data for p,data in prior.items())
    if selection=='affected':
        from test_survival.io import logical_glob, resolve_path
        decision=next(logical_glob(policy/'results/policy/release-reset/decisions','*/*/*.json'))
        saved=read_json(decision);resolve_path(decision).unlink()
        call('policy-repair',['policy','run',policy])
        assert read_json(decision)==saved
        assert all(p.read_bytes()==data for p,data in prior.items())
    (root/'verification.json').write_text(json.dumps(dict(passed=True,pool_methods=len(pool),funnel=funnel,timings=timings),indent=2)+'\n')
    print(root/'verification.json')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--selection',choices=['all','affected'],default='all')
    args=parser.parse_args();main(args.root,args.selection)
