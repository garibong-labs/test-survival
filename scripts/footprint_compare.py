#!/usr/bin/env python3
"""Audit the disposable footprint fixture; never rewrite experiment evidence.

All logical paths are inventoried. Scientific artifacts compare in order, without
rounding or filtering methods. Operational histories have separate checks below.
The JSON report includes every path and every exception applied.
"""
import csv
from collections import Counter, defaultdict
import difflib
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

from test_survival.io import compressed_result, read_bytes, read_json, result_hash
from test_survival.adapters.compile_events import read_events, read_event
from test_survival.config import canonical_hash

ROOT = Path('/tmp/footprint-e2e').resolve()
TIMES = {'started','finished','imported','timestamp','checked_at','archived_utc'}
DURATIONS = {'seconds','elapsed_seconds','phase_seconds','latency'}
UUID = re.compile(r'(?<![a-f0-9])[a-f0-9]{32}(?![a-f0-9])')


def category(name):
    parts=Path(name).parts
    if 'work' in parts:return 'scratch/cache'
    if 'compile-events' in parts:return 'compile lifecycle'
    if ('state' in parts and (set(parts)&{'containers','workers'} or parts[-1] in {'log.txt','run.json','stop.json','stop.lock','run-owner.json'}
                             or parts[-1].startswith('workspace-') or parts[-1].endswith('-error.txt'))):return 'run lifecycle'
    if 'workspace-identities' in parts or 'workspace-restorations' in parts or parts[-1]=='work-cleanup.json':return 'workspace lifecycle'
    if 'results' in parts and (parts[-1].endswith('.txt') and 'baseline-reports' not in parts or 'logs' in parts):return 'command logs'
    return 'compare'


def manifest(root):
    rows={}
    for path in sorted(root.rglob('*')):
        if not path.is_file():continue
        name=str(path.relative_to(root))
        logical=name[:-3] if name.endswith('.gz') and compressed_result(path) else name
        row=dict(physical=name,bytes=path.stat().st_size,category=category(logical))
        row['sha256']=result_hash(path) if compressed_result(path) else hashlib.sha256(path.read_bytes()).hexdigest()
        if logical in rows:
            assert row['sha256']==rows[logical]['sha256'],f'ambiguous twins: {logical}'
        rows[logical]=row
    return rows


class Comparison:
    def __init__(self, root, tool):
        self.root=root;self.tool=tool;self.exceptions=Counter();self.events={};self.maps=[]
        self.source_hashes=defaultdict(set)
        for path in (tool/'src/test_survival').rglob('*'):
            if path.is_file():self.source_hashes[path.name].add(hashlib.sha256(path.read_bytes()).hexdigest())

    def exp(self,name):
        parts=Path(name).parts
        # Results inside a pool belong to that pool, not the policy coordinator.
        if 'pools' in parts:
            n=parts.index('pools')
            if len(parts)>n+3:return self.root/Path(*parts[:n+3])
        return self.root/parts[0]

    def exception(self,kind,value='<volatile>'):
        self.exceptions[kind]+=1;return value

    def string(self,value):
        # Known Docker/host tool mount roots; fixture paths themselves are equal.
        for root in ('/survival-package',str(self.tool/'src/test_survival')):
            value=value.replace(root,'<TOOL>')
        return UUID.sub('<UUID>',value)

    def json(self,value,name,key=''):
        if name.endswith('/results/figure-render.json') and key=='cwd':
            assert value in ('/experiment/work/checkouts/0','/experiment')
            return self.exception('renderer absolute working directory')
        if key in TIMES:return self.exception('timestamp:'+key)
        if key in DURATIONS:return self.exception('duration:'+key)
        if key in ('pid','pgid','worker_pid','run_id','container','container_id'):
            return self.exception('process identity:'+key)
        if key=='runner_sha256' and isinstance(value,dict):
            for filename,digest in value.items():
                assert digest in self.source_hashes[filename],f'unverified provenance: {name}: {filename}'
            self.maps.append(value)
            return self.exception('verified tool provenance','<verified tool sources>')
        if key=='cache' and isinstance(value,dict):
            if value.get('event'):
                rows=read_event(self.exp(name),value['event'])
                assert any(r['type']=='start' for r in rows),f'missing compile start: {name}'
                assert any(r['type']=='finish' for r in rows),f'missing compile finish: {name}'
            return self.exception('compile cache history','<verified compile reference>')
        if key=='input_sha256' and '/validation/' in name:
            raw=read_json(self.root/name)
            source=self.exp(name)/raw['file']
            # Screen identity includes tool source provenance; validate the key.
            candidates=[]
            for mapping in self.maps:
                candidates.append(canonical_hash(dict(source=hashlib.sha256(source.read_bytes()).hexdigest(),repeats=3,runner=mapping)))
            # Provenance maps are collected before comparisons, including raw results.
            assert value in candidates,f'unverified screen fingerprint: {name}'
            return self.exception('verified screen tool fingerprint')
        if isinstance(value,dict):return {k:self.json(v,name,k) for k,v in value.items()}
        if isinstance(value,list):return [self.json(v,name,key) for v in value]
        if isinstance(value,str):return self.string(value)
        return value

    def normalized(self,name):
        content=read_bytes(self.root/name)
        if name.endswith('.json'):
            return (json.dumps(self.json(json.loads(content),name),indent=2,ensure_ascii=False)+'\n').encode()
        if name.endswith('.csv') and Path(name).name=='dev-method-coverage-t.csv':
            reader=csv.DictReader(io.StringIO(content.decode()));rows=list(reader)
            for row in rows:row['seconds']=self.exception('duration:method coverage seconds')
            output=io.StringIO(newline='');writer=csv.DictWriter(output,fieldnames=reader.fieldnames,lineterminator='\n');writer.writeheader();writer.writerows(rows)
            return output.getvalue().encode()
        if name.endswith('.xml'):
            text=content.decode()
            if '<sessioninfo ' in text:
                text=re.sub(r'<sessioninfo\s+id="[^"]*"\s+start="\d+"\s+dump="\d+"\s*/>', '<sessioninfo id="SESSION" start="0" dump="0"/>',text)
                self.exceptions['JaCoCo session identity/timestamps']+=1
            if 'baseline-reports' in name:
                text=re.sub(r' time="[^"]*"',' time="DURATION"',text)
                text=re.sub(r'(<property name="sun.java.command" value=")[^"]*("/>)',r'\1<SUREFIRE_PROCESS>\2',text)
                text=re.sub(r'(<property name="surefire.real.class.path" value=")[^"]*("/>)',r'\1<SUREFIRE_PROCESS>\2',text)
                self.exceptions['JUnit duration and Surefire process paths']+=1
            return self.string(text).encode()
        if name.endswith('.txt') and 'baseline-reports' in name:
            return re.sub(rb'Time elapsed: [0-9.]+ s',b'Time elapsed: DURATION s',content)
        return content

    def preload(self,names):
        for name in names:
            if name.endswith('.json'):
                def walk(value):
                    if isinstance(value,dict):
                        if isinstance(value.get('runner_sha256'),dict):self.json(value['runner_sha256'],name,'runner_sha256')
                        for v in value.values():walk(v)
                    elif isinstance(value,list):
                        for v in value:walk(v)
                walk(read_json(self.root/name))


def audit_events(root):
    counts={}
    for directory in root.rglob('compile-events'):
        rows=read_events(directory);events=defaultdict(list)
        for row in rows:events[row['event_id']].append(row)
        semantics=[]
        for ident,records in events.items():
            types=[r['type'] for r in records]
            assert types.count('start')==1, (directory,ident,types)
            assert types.count('finish')+types.count('interrupted')<=1,(directory,ident,types)
            start=next(r for r in records if r['type']=='start')
            finish=next((r for r in records if r['type']=='finish'),None)
            if finish:
                result=finish['result'];log=finish.get('javac',{})
                if not log.get('truncated'):
                    assert hashlib.sha256(log.get('text','').encode()).hexdigest()==log.get('sha256')
                semantics.append(dict(context=start['context'],command=start['command'],
                                      exit_code=result['exit_code'],timeout=result.get('timeout',False),
                                      javac_bytes=log['bytes'],javac_sha256=log['sha256']))
            for row in records:
                origin=row.get('result',{}).get('cache',{}).get('origin_event')
                if origin:assert origin in events, ('missing cache origin',directory,origin)
                if 'javac' in row and row['javac'].get('truncated'):
                    assert len(row['javac']['text'].encode())<=16384
        counts[str(directory.relative_to(root))]=dict(semantics=sorted(semantics,key=lambda x:json.dumps(x,sort_keys=True)),events=len(events),records=len(rows),unfinished=sum(not any(r['type'] in ('finish','interrupted') for r in rs) for rs in events.values()))
    return counts


def audit_workspace(root):
    restored=[]
    for path in root.rglob('workspace-restorations/*/*/result.json'):
        exp=path.parents[4]
        index=path.parents[1].name
        original=read_json(exp/f'results/builds/{index}.json')
        restored_result=read_json(path)
        assert restored_result['sha']==original['sha'] and restored_result['build']==original['build']=='ok'
        identity=read_json(exp/f'results/workspace-identities/{index}.json')
        assert read_json(path.parent/f'{index}-identity/dependencies.json')==identity['dependencies']
        restored.append(str(path.relative_to(root)))
    return restored


def command_logs(root, manifest):
    return {UUID.sub('<UUID>',name):read_bytes(root/name) for name,row in manifest.items()
            if row['category']=='command logs' and name.endswith('.txt') and 'workspace-restorations' not in name}


def compare(left,right,left_tool,right_tool):
    lroot=ROOT/left/'experiments';rroot=ROOT/right/'experiments'
    lm,rm=manifest(lroot),manifest(rroot)
    names=sorted({n for n,r in lm.items() if r['category']=='compare'}|{n for n,r in rm.items() if r['category']=='compare'})
    lc,rc=Comparison(lroot,Path(left_tool).resolve()),Comparison(rroot,Path(right_tool).resolve())
    lc.preload(n for n in names if n in lm);rc.preload(n for n in names if n in rm)
    missing=[];different=[];equal=[];diffs=[]
    for name in names:
        if name not in lm or name not in rm:missing.append(name);continue
        a,b=lc.normalized(name),rc.normalized(name)
        if a==b:equal.append(name)
        else:
            different.append(name)
            if not name.endswith('.png'):
                diffs.extend(difflib.unified_diff(a.decode(errors='replace').splitlines(True),b.decode(errors='replace').splitlines(True),fromfile=left+'/'+name,tofile=right+'/'+name))
    event_a,event_b=audit_events(lroot),audit_events(rroot)
    event_differences=[name for name in sorted(set(event_a)|set(event_b))
                       if {json.dumps(r,sort_keys=True) for r in event_a.get(name,{}).get('semantics',[])} !=
                          {json.dumps(r,sort_keys=True) for r in event_b.get(name,{}).get('semantics',[])}]
    logs_a,logs_b=command_logs(lroot,lm),command_logs(rroot,rm)
    log_differences=[name for name in logs_a if name not in logs_b or logs_a[name]!=logs_b[name]]
    report=dict(command_logs_checked=len(logs_a),command_log_differences=log_differences,restorations={left:audit_workspace(lroot),right:audit_workspace(rroot)},event_differences=event_differences,left=left,right=right,equal=equal,missing=missing,different=different,
                exceptions={left:dict(lc.exceptions),right:dict(rc.exceptions)},
                manifests={left:lm,right:rm},events={left:event_a,right:event_b})
    destination=ROOT/f'comparison-{left}-{right}'
    destination.with_suffix('.json').write_text(json.dumps(report,indent=2))
    destination.with_suffix('.diff').write_text(''.join(diffs))
    print(json.dumps(dict(equal=len(equal),missing=missing,different=different,event_differences=event_differences,command_log_differences=log_differences),indent=2))
    return bool(missing or different or event_differences or log_differences)


if __name__=='__main__':sys.exit(compare(*sys.argv[1:]))
