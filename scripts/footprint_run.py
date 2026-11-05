#!/usr/bin/env python3
"""Run one disposable footprint comparison, retaining full evidence outside target.

Usage: footprint_run.py TOOL_CHECKOUT LABEL [--interrupt]
Requires footprint_fixture.py setup and footprint-e2e-seed Maven volume.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path('/tmp/footprint-e2e').resolve()
TARGET = ROOT/'target'


def sample():
    data = {}
    for exp in (TARGET/'experiments').iterdir():
        if not exp.is_dir(): continue
        state = {}
        for p in (exp/'state').glob('*.json'):
            if p.name.startswith(('policy-run-', 'policy-pool-')): continue
            try:
                r=json.loads(p.read_text())
                if r.get('status') == 'running':state[p.stem]=r.get('started')
            except (ValueError,FileNotFoundError):pass
        roots = [exp/'work/checkouts', *exp.glob('results/policy/pools/*/*/work/checkouts')]
        count=files=size=0
        for root in roots:
            if not root.exists():continue
            for index in list(root.iterdir()):
                if not index.is_dir():continue
                count+=1
                for folder,dirs,names in os.walk(index):
                    for name in names:
                        try:size+=(Path(folder)/name).stat().st_size;files+=1
                        except FileNotFoundError:pass
        data[exp.name]=dict(stages=sorted(state),checkouts=count,files=files,bytes=size)
    return data


def run(tool,label,interrupt=False):
    saved=ROOT/label
    if saved.exists():raise SystemExit('Evidence label already exists')
    saved.mkdir()
    # Preserve previous run; never delete its experiment evidence.
    experiments=TARGET/'experiments'
    old=ROOT/f'prior-{label}'
    if experiments.exists():experiments.rename(old)
    for name in ('baseline','policy'):
        dest=experiments/name;dest.mkdir(parents=True)
        shutil.copy2(old/name/'experiment.yaml',dest/'experiment.yaml')
    subprocess.run(['docker','run','--rm','-v','footprint-e2e-seed:/seed:ro','-v','footprint-e2e-m2:/root/.m2',
                    'survival-java:25','sh','-c','find /root/.m2 -mindepth 1 -maxdepth 1 -exec rm -rf {} +; cp -a /seed/. /root/.m2/'],check=True)
    env=dict(os.environ,PYTHONPATH=str(Path(tool).resolve()/'src'))
    cli=[sys.executable,'-m','test_survival.cli']
    commands=[['run',str(experiments/'baseline'),'--until','scope','--no-panel'],
              ['import',str(experiments/'baseline'),'--from',str(ROOT/'generated-fixture'),'--what','generated'],
              ['run',str(experiments/'baseline'),'--until','report','--no-panel'],
              ['policy','run',str(experiments/'policy'),'--until','report']]
    start=time.monotonic();samples=[];timings=[];stopped=False
    for number,args in enumerate(commands):
        command_start=time.monotonic()
        with (saved/f'command-{number}.log').open('w') as log:
            child=subprocess.Popen(cli+args,env=env,stdout=log,stderr=subprocess.STDOUT)
            while child.poll() is None:
                elapsed=time.monotonic()-start
                if elapsed>3600:
                    subprocess.run(cli+['stop',str(experiments/('policy' if number==3 else 'baseline'))],env=env)
                    raise SystemExit('Reference/branch exceeded one hour; stop and shrink fixture')
                samples.append(dict(seconds=elapsed,experiments=sample()))
                if interrupt and number==3 and not stopped:
                    work=experiments/'policy/work/policy-survive'
                    # Real worker has created its batch and launcher manifest, not merely a job input.
                    active=list(work.glob('*/batches.tsv')) if work.exists() else []
                    if active:
                        stop=subprocess.run(cli+['stop',str(experiments/'policy')],env=env,capture_output=True,text=True)
                        (saved/'stop.log').write_text(stop.stdout+stop.stderr)
                        if stop.returncode:raise RuntimeError('stop failed')
                        child.wait(timeout=30)
                        (saved/'interruption.json').write_text(json.dumps(dict(sample=sample(),work=[str(p.relative_to(experiments)) for p in active],run=json.loads((experiments/'policy/state/run.json').read_text())),indent=2))
                        shutil.copytree(experiments/'policy/state',saved/'interrupted-state')
                        stopped=True
                        child=subprocess.Popen(cli+args,env=env,stdout=log,stderr=subprocess.STDOUT)
                time.sleep(.15)
        timings.append(dict(command=args,seconds=round(time.monotonic()-command_start,3),exit_code=child.returncode))
        if child.returncode:raise RuntimeError(f'Command {number} failed: {saved}/command-{number}.log')
    if interrupt and not stopped:raise RuntimeError('Failed to observe interruption window')
    (saved/'timings.json').write_text(json.dumps(dict(total_seconds=time.monotonic()-start,commands=timings),indent=2))
    (saved/'samples.json').write_text(json.dumps(samples))
    shutil.copytree(experiments,saved/'experiments')
    (saved/'tool-sha.txt').write_text(subprocess.check_output(['git','-C',tool,'rev-parse','HEAD'],text=True))
    print(json.dumps(dict(label=label,seconds=time.monotonic()-start,interrupted=stopped)),flush=True)


if __name__=='__main__':run(sys.argv[1],sys.argv[2],'--interrupt' in sys.argv[3:])
