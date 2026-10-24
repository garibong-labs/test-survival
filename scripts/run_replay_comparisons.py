"""Finish the authorized replay, then run the separate single-worker comparison."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import yaml
from test_survival.config import load,stage_hash
from test_survival.io import read_json,write_json,now
from test_survival.pipeline import process_alive
from migrate_state_hashes import migrate


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--replay',type=Path,required=True)
    parser.add_argument('--reference',type=Path,required=True)
    parser.add_argument('--serial',type=Path,required=True)
    parser.add_argument('--resume-serial',action='store_true',help='Wait for an active serial stage and finish only its remaining stages')
    parser.add_argument('--workspace-root',type=Path,help='Evidence destination when using an isolated committed runtime')
    parser.add_argument('--runtime-commit')
    parser.add_argument('--runtime-path',type=Path)
    args=parser.parse_args()
    root=args.workspace_root or Path(__file__).resolve().parents[1]
    scripts=Path(__file__).resolve().parent
    evidence=root/'tests/evidence'
    with (evidence/'comparison-driver.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state=dict(pid=os.getpid(),status='running',started=now(),replay=str(args.replay),serial=str(args.serial))
        if args.runtime_commit:
            state.update(runtime_commit=args.runtime_commit,runtime_path=str(args.runtime_path))
        def checkpoint(step):
            state.update(step=step,updated=now());write_json(evidence/'comparison-driver.json',state)
            print(json.dumps({'time':now(),'step':step}),flush=True)
        def command(*argv):
            subprocess.run(list(map(str,argv)),check=True,cwd=root)
        def run(exp,*options):
            command(sys.executable,'-m','test_survival.cli','run',exp,*options)
        def migrate_completed(exp, stages):
            migrate(exp, stages, evidence/'state-hash-migration.json')
        def compare(include_serial):
            command(sys.executable,scripts/'compare_full_replay.py','--reference',args.reference,
                    '--replay','Configured replay='+str(args.replay),
                    *(['--replay','Single-worker replay='+str(args.serial)] if include_serial else []),
                    '--out',root/'.local/acceptance-report.md','--json-out',evidence/'full-comparisons.json',
                    *(['--runtime-commit',args.runtime_commit,'--runtime-path',args.runtime_path] if args.runtime_commit else []))
        try:
            if args.resume_serial:
                checkpoint('waiting for the active serial run without restarting it')
                while True:
                    active=read_json(args.serial/'state/run.json',{})
                    if active.get('status')!='running':break
                    if not process_alive(active.get('pid')):
                        raise RuntimeError('Active serial process stopped without finishing its record')
                    time.sleep(10)
                config=load(args.serial)
                if config['runner']['workers']['survive']!=1:
                    raise RuntimeError('Serial copy must have runner.workers.survive=1')
                for stage in ['survive','cover','report']:
                    record=read_json(args.serial/f'state/{stage}.json',{})
                    if record.get('status')=='completed' and record.get('config_hash')==stage_hash(config,stage):continue
                    checkpoint('single-worker replay: '+stage)
                    run(args.serial,'--stage',stage,'--force')
                checkpoint('both complete step-6 comparisons')
                compare(True)
                state.update(status='completed',finished=now());write_json(evidence/'comparison-driver.json',state)
                return
            checkpoint('waiting for the active replay run without restarting it')
            while True:
                active=read_json(args.replay/'state/run.json',{})
                if active.get('status')!='running':break
                if not process_alive(active.get('pid')):
                    raise RuntimeError('Active replay process stopped without finishing its record')
                time.sleep(10)
            # The active process loaded the former hashing implementation. Its
            # completed record must be migrated before a new process resumes.
            migrate_completed(args.replay,['prepare','scope','generate','screen','control','survive'])
            checkpoint('configured replay: coverage and report')
            if read_json(args.replay/'state/cover.json',{}).get('status')!='completed':
                run(args.replay,'--stage','cover')
            migrate_completed(args.replay,['cover'])
            run(args.replay,'--stage','report')
            checkpoint('configured replay: complete step-6 comparison')
            compare(False)
            checkpoint('copying the replay for the single-worker run')
            marker=args.serial/'state/comparison-copy.json'
            owned=read_json(marker,{})
            if args.serial.exists() and (owned.get('source')!=str(args.replay) or owned.get('purpose')!='single-worker comparison'):
                raise RuntimeError('Serial destination is not this driver-owned copy')
            if not owned or owned.get('status')=='copying':
                owned=dict(source=str(args.replay),purpose='single-worker comparison',status='copying',started=now())
                write_json(marker,owned)
                shutil.copytree(args.replay,args.serial,dirs_exist_ok=True)
                config=yaml.safe_load((args.serial/'experiment.yaml').read_text())
                config['runner']['workers']['survive']=1
                (args.serial/'experiment.yaml').write_text(yaml.safe_dump(config,sort_keys=False,allow_unicode=True))
                owned.update(status='ready',finished=now());write_json(marker,owned)
            if load(args.serial)['runner']['workers']['survive']!=1:
                raise RuntimeError('Serial copy must have runner.workers.survive=1')
            # Preserve the copied, frozen inputs. Only survival concurrency has
            # changed; all three measurements below are explicitly forced.
            checkpoint('single-worker copy: migrating frozen prerequisite hashes')
            migrate_completed(args.serial,['prepare','scope','generate','screen','control'])
            command(sys.executable,root/'scripts/check_acceptance.py',args.serial,args.replay,'--checkpoint','screen','--out',evidence/'serial-denominator-comparison.json')
            checkpoint('single-worker replay: survive')
            run(args.serial,'--stage','survive','--force')
            checkpoint('single-worker replay: cover')
            run(args.serial,'--stage','cover','--force')
            checkpoint('single-worker replay: report')
            run(args.serial,'--stage','report','--force')
            checkpoint('both complete step-6 comparisons')
            compare(True)
            state.update(status='completed',finished=now());write_json(evidence/'comparison-driver.json',state)
        except BaseException as exc:
            state.update(status='failed',finished=now(),error_type=type(exc).__name__)
            write_json(evidence/'comparison-driver.json',state)
            raise


if __name__=='__main__':main()
