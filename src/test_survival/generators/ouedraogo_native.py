from concurrent.futures import ThreadPoolExecutor
import hashlib
import time

from .base import RoundResult
from ..config import generator_config
from .parts import context,prompts,postprocess
from .parts.extraction import extract
from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json,write_json,atomic_text,now
from ..model.openrouter import OpenRouter
from ..pipeline import log


class OuedraogoNative:
    def __init__(self,exp,config,generator):
        self.exp,self.config,self.group=exp,generator_config(config,generator),generator
        self.adapter=JavaMavenAdapter(exp,self.config)

    def generate(self,round_no):
        label=f'round{round_no}'
        directory=self.exp/'generated'/self.group['name']/label
        directory.mkdir(parents=True,exist_ok=True)
        scope=read_json(self.exp/'population/scope.json')['classes']
        settings=self.group['context']['dependency_signatures']
        data=context.inventory(self.adapter,directory) if settings['enabled'] or self.group['postprocess']['import_repair']['enabled'] else None
        jobs=[];blocks=[]
        for row in scope:
            name=row['class'].rsplit('.',1)[-1]
            source=(self.adapter.folder(0)/row['file']).read_text()
            block=''
            if settings['enabled']:
                block,metadata=context.dependency_block(data,row['class'],source,settings)
                dest=directory/'prompts'/name
                atomic_text(dest/'dependency-signatures.txt',block)
                write_json(dest/'dependency-block.json',metadata)
                write_json(dest/'dependency-manifest.json',metadata)
                blocks.append(metadata)
            for technique in self.group['params']['prompts']:
                rendered=prompts.render(technique,name,source,block,self.group['prompt']['api_constraint'])
                path=directory/'prompts'/name/(technique+'.txt')
                if path.exists() and path.read_text()!=rendered and (directory/'runs').exists():
                    raise RuntimeError('Cannot change a prompt after its model call')
                atomic_text(path,rendered)
                jobs.append((name,technique,rendered))
        write_json(directory/'prompts/pre-call-check.json',dict(passed=True,checked_at=now(),blocks=blocks,empty_angle_brackets=False,headers_have_modifiers=True))
        client=OpenRouter(self.config['model'])
        records=read_json(directory/'generation.json')
        if records is not None:
            # Model work is committed before import repair; check its inputs but
            # never invoke the model client for an already recorded round.
            for name,technique,prompt in jobs:
                client.verify(prompt,self.group['params'],directory/'runs'/name/technique)
            return self.finish(directory,records)
        def call(job):
            name,technique,prompt=job
            folder=directory/'runs'/name/technique
            saved=read_json(directory/'records'/f'{name}-{technique}.json')
            if saved is not None:
                client.verify(prompt,self.group['params'],folder)
                return saved
            attempts=[]
            for attempt in (1,2):
                record,text=client.complete(prompt,self.group['params'],folder if attempt==1 else folder/'attempt-2')
                log(self.exp, f'generate: {self.group["name"]} {label} {name} {technique} call {attempt} recorded', self.config)
                attempts.append(record)
                if record['status']=='ok':break
                if attempt==1:time.sleep(60)
            extracted=extract(text)
            file=None
            if extracted['csr_v2']:
                file=directory/'sources'/name/technique/(extracted['class_name']+'.java')
                if not file.exists():atomic_text(file,extracted['combine_v2'])
            result=dict(class_name=name,technique=technique,status=record['status'],attempts=len(attempts),calls=attempts,
                        extraction={k:v for k,v in extracted.items() if k!='combine_v2'},file=str(file.relative_to(self.exp)) if file else None)
            # The record is the commit point for a call, so its raw response is published first.
            write_json(directory/'raw-responses'/f'{name}-{technique}.json',read_json((folder if len(attempts)==1 else folder/'attempt-2')/'raw-response.json'))
            write_json(directory/'records'/f'{name}-{technique}.json',result)
            from ..panel import notify
            notify(self.exp)
            return result
        with ThreadPoolExecutor(max_workers=2) as pool:records=list(pool.map(call,jobs))
        write_json(directory/'generation.json',records)
        return self.finish(directory,records)

    def finish(self,directory,records):
        if self.group['postprocess']['import_repair']['enabled']:
            self.adapter.setup_runner()
            postprocess.repair(self.adapter,directory,self.group['postprocess']['import_repair']['passes'])
        return RoundResult(files=[self.exp/r['file'] for r in records if r['file']],calls=[c for r in records for c in r['calls']])
