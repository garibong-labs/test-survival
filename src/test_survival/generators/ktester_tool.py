from pathlib import Path
import os
import subprocess

from .base import RoundResult
from ..config import generator_config
from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json,write_json
from ..model.credentials import _read
from ..lifecycle import launch
from ..rules.ktester_round import fully_visited
from ..importer import rounds


def round_result(directory, *, reuse=False):
    """Flatten a new round, or finish only missing copies in an archived round."""
    sources=sorted(directory.rglob('test_classes/*.java'))
    flat=directory/'sources';flat.mkdir(parents=True,exist_ok=True)
    import shutil
    # Preserve the old sorted-copy winner when filenames collide, without
    # overwriting already published sources when revisiting a completed round.
    for name,file in {file.name:file for file in sources}.items():
        if not reuse or not (flat/name).exists():shutil.copy2(file,flat/name)
    calls=[read_json(p) for p in sorted((directory/'raw-responses').glob('*/call.json'))]
    return RoundResult(files=sources,calls=calls)


def verified_archives(root, maximum):
    manifest = read_json(root/'rounds.json', {})
    active = rounds(root, maximum)
    recorded = {r.get('directory') or f"round{r['round']}": r.get('funnel', {}).get('post_process')
                for r in manifest.get('rounds', [])}
    if not manifest.get('stopped') or not active or set(recorded) != {d.name for d in active}:
        return False
    for directory in active:
        run = read_json(directory/'run.json', {})
        if not fully_visited(run) or run['post_process'] != recorded[directory.name]:
            return False
        if read_json(directory/'funnel.json', {}).get('post_process') != run['post_process']:
            return False
    return True


class KTesterTool:
    def __init__(self,exp,config,generator):
        self.exp,self.config,self.group=exp,generator_config(config,generator),generator
        self.adapter=JavaMavenAdapter(exp,self.config)

    def generate(self,round_no):
        label=f'round{round_no}'
        directory=self.exp/'generated'/self.group['name']/label
        # Whole-run/pool cleanup removes disposable preparation and worker
        # markers. The published round must win before any worker or key access.
        stale = []
        if round_no == 1:
            for archived in rounds(directory.parent, self.group['rounds']['max']):
                run = read_json(archived/'run.json', {})
                if run.get('status') == 'completed' and not fully_visited(run):
                    stale.append(archived.name)
        run = read_json(directory/'run.json', {})
        if directory.exists() and run.get('status') != 'completed':
            raise RuntimeError('KTester archive is incomplete or failed; inspect '+str(directory))
        reusable = fully_visited(run)
        if reusable and not stale:
            return round_result(directory, reuse=True)
        project_name=Path(self.config['project']['repo']).name
        script=Path(__file__).parents[1]/'adapters/ktester_worker.py'
        job=dict(experiment=self.adapter.runner.path(self.exp),module=self.config['project']['module'],tool=self.adapter.runner.path(self.group['tool_path']),project_name=project_name,group=self.group['name'],round=round_no,retry=0,label=label,params=self.group['params'],model=self.config['model']['name'],env_key=self.config['model']['env_key'])
        if stale:
            # A regenerated archive may exist before its replacement screening is published.
            manifest = directory.parent/'rounds.json'
            previous = read_json(manifest, {})
            if previous.get('stopped'):
                write_json(manifest, {**previous, 'stopped': None})
        job['superseded'] = stale
        actions = ['retire'] if stale else []
        if not reusable:
            actions += ['prepare', 'index', 'dataset', 'generate']
        for action in actions:
            state=self.exp/'work/ktester'/self.group['name']/(action+('-'+label if action=='generate' else '')+'.json')
            if action in ('prepare', 'index', 'dataset') and read_json(state,{}).get('status')=='completed':continue
            job['action']=action;path=state.with_suffix('.job.json');write_json(path,job)
            command=self.adapter.runner.command(['python3',str(script),str(path)],self.adapter.folder(0))
            env=None
            if action=='generate':
                key=_read(self.config['model'])
                if not key:raise RuntimeError('KEY_MISSING')
                env=os.environ.copy();env[self.config['model']['env_key']]=key
                # Pass the variable name only; Docker reads its value from this child environment.
                target=self.config['runner']['docker']['service'] if self.config['runner']['docker']['compose_files'] else self.config['runner']['docker']['image']
                pos=command.index(target);command[pos:pos]=['--env',self.config['model']['env_key']]
            with state.with_suffix('.log').open('w') as stream:
                with launch(self.exp,command,env=env,stdout=stream,stderr=subprocess.STDOUT) as result:
                    result.wait()
            verified = action != 'generate' or fully_visited(read_json(directory/'run.json', {}))
            write_json(state,dict(status='completed' if result.returncode==0 and verified else 'failed',exit_code=result.returncode))
            if not verified:
                record = read_json(directory/'run.json', {})
                post = record.get('post_process') or {}
                detail = post.get('error') or record.get('error_type') or 'Missing full post-process record'
                raise RuntimeError(f'KTester {label}: {detail}; see {state.with_suffix(".log")}')
            if result.returncode:raise RuntimeError('KTester '+action+' failed; archived output retained')
        return round_result(directory, reuse=reusable)
