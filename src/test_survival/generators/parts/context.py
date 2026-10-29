"""Ordered project dependencies; source-faithful JDK API declarations."""
from collections import defaultdict
from pathlib import Path
import re

from ...io import write_json
from .prompts import gate


def inventory(adapter,directory):
    log=directory/'prompts/source-api-inventory.tsv'
    result=adapter.runner.execute(['java',str(Path(__file__).with_name('java')/'DependencyInventory2.java'),str(adapter.folder(0)/'src/main/java')],adapter.folder(0),log,adapter.config['runner']['timeouts']['compile'])
    if result['exit_code']:raise RuntimeError('Dependency inventory failed')
    skipped=[]
    types={};imports=defaultdict(list);refs=defaultdict(lambda:defaultdict(list));apis=defaultdict(list)
    for line in log.read_text().splitlines():
        p=line.split('\t')
        if len(p)<2:continue
        kind,fqn=p[:2]
        if kind=='T':types[fqn]=dict(package=p[2],kind=p[3],modifiers=p[4],parameters=p[5],extends=p[6],implements=p[7],components=p[8] if len(p)>8 else '',permits=p[9] if len(p)>9 else '')
        elif kind=='I':imports[fqn].append(p[2])
        elif kind=='R':refs[fqn][p[2]].append(p[3])
        elif kind=='A':apis[fqn].append(p[2])
        elif kind=='S':skipped.append(dict(class_name=fqn,file=p[2],reason=p[3]))
    write_json(directory/'prompts/type-inventory.json',types)
    write_json(directory/'prompts/skipped-types.json',skipped)
    return types,imports,refs,apis


def dependency_block(data,fqn,source,limits):
    types,imports,refs,apis=data
    def resolve(owner,expr):
        found=[]
        for token in re.findall(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*',expr):
            candidates=[]
            if token in types:candidates.append(token)
            for imp in imports[owner]:
                if imp.endswith('.*'):cand=imp[:-1]+token
                else:
                    first,*tail=token.split('.')
                    cand=imp+('.'+'.'.join(tail) if tail else '') if imp.rsplit('.',1)[-1]==first else ''
                if cand in types:candidates.append(cand)
            for prefix in [owner,types.get(owner,{}).get('package',fqn.rpartition('.')[0])]:
                if prefix+'.'+token in types:candidates.append(prefix+'.'+token)
            candidates=list(dict.fromkeys(candidates))
            if len(candidates)==1 and candidates[0] not in found:found.append(candidates[0])
        return found
    ordered=[];reasons={};visited=set()
    def add(t,why):
        if t!=fqn and t not in ordered:ordered.append(t);reasons[t]=why
    def supers(t):
        if t in visited:return
        visited.add(t)
        for expr in refs[t]['super']:
            for dep in resolve(t,expr):add(dep,'superclass/interface chain');supers(dep)
    supers(fqn)
    for category in ['constructor','field','method']:
        for expr in refs[fqn][category]:
            for dep in resolve(fqn,expr):add(dep,category)
    for imp in imports[fqn]:
        if imp in types:add(imp,'import')
        elif imp.endswith('.*'):
            for token in dict.fromkeys(re.findall(r'\b[A-Za-z_$][\w$]*\b',source)):
                if imp[:-1]+token in types:add(imp[:-1]+token,'wildcard import reference')
    for nested in types:
        if nested.startswith(fqn+'.'):
            add(nested,'nested declaration')
            for category in ['super','constructor','field','method']:
                for expr in refs[nested][category]:
                    for dep in resolve(nested,expr):add(dep,'nested '+category)
    blocks=[];kept=[];omitted=[];lines=0
    for dep in ordered:
        t=types[dep];simple=dep.rsplit('.',1)[-1]
        header=(t['modifiers']+' '+('@interface' if t['kind']=='ANNOTATION_TYPE' else t['kind'].lower())+' '+simple+('<'+t['parameters']+'>' if t['parameters'] else '')).strip()
        if t['kind']=='RECORD':header+='('+t.get('components','')+')'
        if t['extends']:header+=' extends '+t['extends']
        if t['implements']:header+=(' extends ' if t['kind']=='INTERFACE' else ' implements ')+t['implements']
        if t.get('permits'):header+=' permits '+t['permits']
        block=['// public API of '+dep,header+' {',*['  '+a for a in apis[dep]],'}','']
        if omitted or len(kept)>=limits['max_types'] or lines+len(block)>limits['max_lines']:omitted.append(dep);continue
        kept.append(dep);blocks.extend(block);lines+=len(block)
    block='\n'.join(blocks)+'\n';gate(block)
    metadata=dict(class_name=fqn,ordered_types=ordered,included=kept,omitted=omitted,reasons=reasons,types=len(kept),lines=len(block.splitlines()),limits=dict(types=limits['max_types'],lines=limits['max_lines']))
    return block,metadata
