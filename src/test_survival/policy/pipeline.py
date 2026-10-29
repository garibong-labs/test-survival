"""Policy transactions, resumability and background execution."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

from ..config import ConfigError, canonical_hash, load, result_inputs
from ..io import now, read_json, write_json
from ..pipeline import PipelineError, run_lock, process_alive
from .baseline import Baseline
from . import plan as planning

STEPS = ('pools','survive','cover','report')


class Steps:
    def __init__(self, baseline, plan):
        self.b = baseline
        self.digest = canonical_hash(dict(config=result_inputs(baseline.execution_config), policies=baseline.options, plan=plan,
                                          frozen=baseline.frozen, survival=baseline.curves))

    def __call__(self, name, action, *, outputs=()):
        path = self.b.guard(self.b.exp/f'state/policy-{name}.json')
        prior = read_json(path, {})
        if prior and prior.get('config_hash') != self.digest:
            raise ConfigError('Policy inputs changed; use --force to rebuild policy evidence')
        if prior.get('status') == 'completed':
            if not all(p.exists() for p in outputs):
                raise PipelineError('Completed policy evidence is missing; restore it or use --force: '+name)
            return
        record = dict(status='running', config_hash=self.digest, started=now(), pid=os.getpid())
        write_json(path, record)
        try:
            action()
        except BaseException:
            write_json(path, {**record,'status':'failed','finished':now()})
            raise
        write_json(path, {**record,'status':'completed','finished':now()})


def run(exp, config, until='report', force=False, lock_fd=None):
    b = Baseline(exp,config)  # Validate the read-only boundary before creating lock/state files.
    with run_lock(b.exp, lock_fd):
        if force:
            root = b.exp/'results/policy'
            if root.exists():
                archive = b.exp/'work/policy-history'/now().replace(':','-')
                archive.parent.mkdir(parents=True,exist_ok=True)
                shutil.move(root,archive)
            for path in (b.exp/'state').glob('policy-*.json'):
                path.unlink()
            # Adapter result caches are beneath this experiment, never the baseline.
            for path in (b.exp/'results/raw').glob('*-policy-*.json'):
                path.unlink()
            directory = b.exp/'results/policy-coverage'
            if directory.exists():
                shutil.rmtree(directory)
        plan = planning.run(b.exp,config)
        step = Steps(b,plan)
        from . import pools, execute, cover, report
        built = {}
        def build():
            built.update(pools.run(b,plan,step))
        pool_outputs = [b.exp/f'results/policy/pools/{p["group"]}/{p["index"]}/pool.json' for p in plan['pools']]
        step('pools',build,outputs=pool_outputs)
        if until == 'pools':
            return
        if not built:
            built = {(p['group'],p['index']):read_json(b.exp/f'results/policy/pools/{p["group"]}/{p["index"]}/pool.json') for p in plan['pools']}
        names = [p['name'] for p in b.options['set']]
        step('survive',lambda:execute.survive(b,plan,built,step),outputs=[b.exp/f'results/policy/{p}/survival.csv' for p in names])
        if until == 'survive':
            return
        step('cover',lambda:cover.run(b,step),outputs=[b.exp/f'results/policy/{p}/coverage-total.json' for p in names])
        if until == 'cover':
            return
        step('report',lambda:report.run(b),outputs=[b.exp/'results/policy/summary.csv',b.exp/'results/policy/consistency.md'])


def background(exp, config, until, force):
    b = Baseline(exp,config)
    with run_lock(b.exp) as fd:
        command = [sys.executable,'-m','test_survival.cli','policy','run',str(b.exp),'--until',until,'--lock-fd',str(fd)]
        if force:
            command.append('--force')
        log = b.exp/'state/policy-background.log'
        with log.open('a') as stream:
            child = subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True,pass_fds=(fd,))
        write_json(b.exp/'state/policy-background.json',dict(pid=child.pid,started=now(),log=str(log)))
        return child.pid,log


def status(exp,config):
    b = Baseline(exp,config)
    records = [(p.name,read_json(p)) for p in sorted((b.exp/'state').glob('policy-*.json'))]
    return '\n'.join(f'{name}: '+(r.get('status','background') if r.get('status') != 'running' or process_alive(r.get('pid')) else 'interrupted') for name,r in records) or 'No policy steps recorded'


def command(args):
    config = load(args.exp_dir)
    if args.policy_command == 'plan':
        b = Baseline(args.exp_dir,config)
        with run_lock(b.exp):
            plan = planning.run(args.exp_dir,config)
        print(f'{len(plan["pools"])} distinct pools; {args.exp_dir}/results/policy/plan.md')
    elif args.policy_command == 'status':
        print(status(args.exp_dir,config))
    elif args.background:
        pid,log = background(args.exp_dir,config,args.until,args.force)
        print(f'PID {pid}; log {log}')
    else:
        run(args.exp_dir,config,args.until,args.force,args.lock_fd)
