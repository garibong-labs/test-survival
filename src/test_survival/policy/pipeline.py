"""Policy transactions, resumability and background execution."""
import os
from pathlib import Path
import shutil
import time
import traceback

from ..config import ConfigError, canonical_hash, load, result_inputs
from ..display import UTC_DISPLAY
from ..io import atomic_text, now, read_json, write_json
from ..pipeline import PipelineError, run_lock, run_session, launch_background, process_alive
from .baseline import Baseline
from . import plan as planning
from .progress import LiveProgress, description

STEPS = ('pools','survive','cover','report')


class Steps:
    def __init__(self, baseline, plan, progress=None):
        self.b = baseline
        self.progress = progress
        self.digest = canonical_hash(dict(evidence_version=3, config=result_inputs(baseline.execution_config), policies=baseline.options, plan=plan,
                                          frozen=baseline.frozen, survival=baseline.curves))

    def __call__(self, name, action, *, outputs=()):
        path = self.b.guard(self.b.exp/f'state/policy-{name}.json')
        prior = read_json(path, {})
        if prior and prior.get('config_hash') != self.digest:
            raise ConfigError('Policy inputs changed; use --force to rebuild policy evidence')
        if prior.get('status') == 'completed':
            if not all(p.exists() for p in outputs):
                raise PipelineError('Completed policy evidence is missing; restore it or use --force: '+name)
            if self.progress:
                self.progress.event(name, 'already completed; skipped')
            return
        error_path = self.b.guard(self.b.exp/f'state/policy-{name}-error.txt')
        error_path.unlink(missing_ok=True)
        record = dict(status='running', config_hash=self.digest, started=now(), pid=os.getpid(),
                      outputs=[str(p.relative_to(self.b.exp)) for p in outputs])
        write_json(path, record)
        if self.progress:
            self.progress.event(name, 'started', running=True)
        try:
            action()
        except BaseException as exc:
            atomic_text(error_path, traceback.format_exc())
            write_json(path, {**record,'status':'failed','finished':now(), 'error':str(error_path)})
            if self.progress:
                self.progress.event(name, f'failed ({type(exc).__name__}); see {error_path}')
            raise
        write_json(path, {**record,'status':'completed','finished':now()})
        if self.progress:
            self.progress.event(name, 'completed')


def run(exp, config, until='report', force=False, lock_fd=None, *, display=UTC_DISPLAY):
    b = Baseline(exp,config)  # Validate the read-only boundary before creating lock/state files.
    with run_lock(b.exp, lock_fd), run_session(b.exp), LiveProgress(b.exp, display=display) as progress:
        if force:
            root = b.exp/'results/policy'
            if root.exists():
                archive = b.exp/'work/policy-history'/now().replace(':','-')
                archive.parent.mkdir(parents=True,exist_ok=True)
                shutil.move(root,archive)
            for path in (b.exp/'state').glob('policy-*.json'):
                path.unlink()
            for path in (b.exp/'state').glob('policy-*-error.txt'):
                path.unlink()
            # Adapter result caches are beneath this experiment, never the baseline.
            for path in (b.exp/'results/raw').glob('*-policy-*.json'):
                path.unlink()
            directory = b.exp/'results/policy-coverage'
            if directory.exists():
                shutil.rmtree(directory)
        plan = planning.run(b.exp,config)
        step = Steps(b,plan,progress)
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
    arguments = ['policy','run',str(b.exp),'--until',until]
    if force:
        arguments.append('--force')
    return launch_background(b.exp, arguments, log_name='policy-background.log')


def status(exp,config, *, display=UTC_DISPLAY):
    b = Baseline(exp,config)
    return _status(b.exp, display=display)


def _status(exp, *, display):
    records = [(p.name,read_json(p)) for p in sorted((exp/'state').glob('policy-*.json'))]
    lines = []
    alive = {}
    for filename, record in records:
        name = filename.removeprefix('policy-').removesuffix('.json')
        state = record.get('status', 'background')
        if state == 'running':
            pid = record.get('pid')
            if pid not in alive:
                alive[pid] = process_alive(pid)
            if not alive[pid]:
                state = 'interrupted'
        progress = description(exp, name, {**record, 'status':state})
        error = f'; see {record["error"]}' if record.get('error') else ''
        lines.append(f'{filename}: {state}{error} | {progress.render(display)}')
    return '\n'.join(lines) or 'No policy steps recorded'


def watch(exp, config, *, display=UTC_DISPLAY):
    # Validate once: repeatedly scanning all nested pool checkouts is expensive.
    b = Baseline(exp, config)
    try:
        while True:
            run = read_json(b.exp/'state/run.json', {})
            print(_status(b.exp, display=display), flush=True)
            if run.get('status') != 'running' or not process_alive(run.get('pid')):
                break
            time.sleep(5)
    except KeyboardInterrupt:
        pass


def command(args, *, display=UTC_DISPLAY):
    config = load(args.exp_dir)
    if args.policy_command == 'plan':
        b = Baseline(args.exp_dir,config)
        with run_lock(b.exp):
            plan = planning.run(args.exp_dir,config)
        print(f'{len(plan["pools"])} distinct pools; {args.exp_dir}/results/policy/plan.md')
    elif args.policy_command == 'status':
        if args.watch:
            watch(args.exp_dir,config,display=display)
        else:
            print(status(args.exp_dir,config,display=display))
    elif args.background:
        pid,log = background(args.exp_dir,config,args.until,args.force)
        print(f'PID {pid}; log {log}')
    else:
        if args.ready_fd is not None:
            try:
                if os.read(args.ready_fd, 1) != b'1':
                    raise PipelineError('Background startup was interrupted')
            finally:
                os.close(args.ready_fd)
        run(args.exp_dir,config,args.until,args.force,args.lock_fd,display=display)
