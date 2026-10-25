from pathlib import Path
import hashlib
import os
import subprocess

from .base import RoundResult
from ..config import generator_config
from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json,write_json
from ..model.credentials import _read
from ..lifecycle import launch


class KTesterTool:
    def __init__(self,exp,config,generator):
        self.exp,self.config,self.group=exp,generator_config(config,generator),generator
        self.adapter=JavaMavenAdapter(exp,self.config)

    def generate(self,round_no):
        label=f'round{round_no}'
        directory=self.exp/'generated'/self.group['name']/label
        project_name=Path(self.config['project']['repo']).name
        script=Path(__file__).parents[1]/'adapters/ktester_worker.py'
        job=dict(experiment=self.adapter.runner.path(self.exp),module=self.config['project']['module'],tool=self.adapter.runner.path(self.group['tool_path']),project_name=project_name,group=self.group['name'],round=round_no,retry=0,label=label,params=self.group['params'],model=self.config['model']['name'],env_key=self.config['model']['env_key'])
        for action in ['prepare','index','dataset','generate']:
            state=self.exp/'work/ktester'/self.group['name']/(action+('-'+label if action=='generate' else '')+'.json')
            if read_json(state,{}).get('status')=='completed':continue
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
            write_json(state,dict(status='completed' if result.returncode==0 else 'failed',exit_code=result.returncode))
            if result.returncode:raise RuntimeError('KTester '+action+' failed; archived output retained')
        sources=sorted(directory.rglob('test_classes/*.java'))
        flat=directory/'sources';flat.mkdir(parents=True,exist_ok=True)
        import shutil
        for file in sources:shutil.copy2(file,flat/file.name)
        calls=[read_json(p) for p in sorted((directory/'raw-responses').glob('*/call.json'))]
        return RoundResult(files=sources,calls=calls)
