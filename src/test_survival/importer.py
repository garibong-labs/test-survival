"""Import archived generated sources or a developer baseline with file provenance."""
import hashlib
from pathlib import Path
import re
import shutil

from .config import load, stage_hash, canonical_hash
from .io import now, read_json, write_json
from .pipeline import PipelineError, invalidate, log, run_lock


def round_key(path):
    match=re.fullmatch(r'round(\d+)(?:-retry(\d*))?',path.name)
    return (int(match[1]),int(match[2] or (1 if '-retry' in path.name else 0))) if match else (10**9,0)


def rounds(group_dir):
    return sorted((p for p in group_dir.glob('round*') if p.is_dir() and re.fullmatch(r'round\d+(?:-retry\d*)?',p.name)),key=round_key)


def import_experiment(exp, source, what):
    exp,source=Path(exp).resolve(),Path(source).resolve()
    if exp==source or exp.is_relative_to(source) or source.is_relative_to(exp):
        raise PipelineError('Import source and destination must be separate experiment folders')
    config=load(exp)
    selections=what.split(',')
    if not selections or set(selections)-{'generated','dev-baseline'}:
        raise PipelineError('--what: allowed values: generated,dev-baseline')
    with run_lock(exp):
        imports=read_json(exp/'state/import.json',{})
        for selection in selections:
            copied={}
            def copy(src,dst):
                if not src.is_file():
                    raise FileNotFoundError(src)
                dst.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(src,dst)
                copied[str(dst.relative_to(exp))]=hashlib.sha256(dst.read_bytes()).hexdigest()
            if selection=='generated':
                source_config=load(source) if (source/'experiment.yaml').exists() else None
                names={g['name'] for g in source_config['generators']} if source_config else {p.name for p in (source/'generated').iterdir() if p.is_dir()}
                imported=[]
                for g in config['generators']:
                    name=g['name']
                    if name not in names:
                        continue
                    src=source/'generated'/name;dest=exp/'generated'/name
                    for f in sorted(src.rglob('*')):
                        if f.is_file():
                            copy(f,dest/f.relative_to(src))
                    entries=[]
                    for directory in rounds(dest):
                        funnel=read_json(directory/'funnel.json')
                        if funnel is None:
                            raise FileNotFoundError(directory/'funnel.json')
                        n,retry=round_key(directory)
                        entries.append(dict(round=n,retry=retry,directory=directory.name,funnel=funnel,source='imported'))
                    if not entries:
                        raise FileNotFoundError(src/'round*')
                    write_json(dest/'rounds.json',dict(source='imported',source_path=str(src),rounds=entries,stopped='imported'))
                    imported.append(name)
                if set(imported)!={g['name'] for g in config['generators']}:
                    # Partial imports are recorded but cannot mark all generation complete.
                    status='pending'
                else:status='completed'
                invalidate(exp,'generate')
                write_json(exp/'state/generate.json',dict(status=status,config_hash=stage_hash(config,'generate'),started=now(),finished=now(),notes='Imported groups: '+', '.join(imported),source_hash=canonical_hash(copied),group_hashes={g['name']:stage_hash(config,'generate',g['name']) for g in config['generators'] if g['name'] in imported}))
            else:
                for name in ['dev-methods-all.csv','dev-denominator.csv']:
                    copy(source/'dev'/name,exp/'dev'/name)
                invalidate(exp,'scope')
                prepare=read_json(exp/'state/prepare.json',{})
                prepare['dev_baseline']=dict(status='completed',source_hash=canonical_hash(copied))
                write_json(exp/'state/prepare.json',prepare)
            imports[selection]=dict(source=str(source),files=copied,source_sha256=canonical_hash(copied),imported=now())
            write_json(exp/'state/import.json',imports)
            log(exp,f'import {selection}: copied {len(copied)} files from {source}')
