from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re

from ..adapters.java_maven import JavaMavenAdapter
from ..importer import rounds
from ..io import read_csv, read_json, write_csv, write_json, atomic_text
from ..pipeline import log
from ..rules.matching import subsample
from ..rules.dev_denominator import frozen_method,denominator_file,validate_derived_subsets

FIELDS=['file','class','method','kind','body_hash','round','technique']


def classification(text):
    diagnostics = re.split(r'(?m)(?=^.*\.java:\d+: error:)', text)
    first = next((d for d in diagnostics if re.match(r'^.*\.java:\d+: error:', d)), None)
    if first is not None:text = first
    if 'cannot find symbol' in text:
        return 'missing-import' if re.search(r'symbol:\s+class\s+',text) else 'unknown-api'
    if any(s in text for s in ['cannot be applied','incompatible types','no suitable method','no suitable constructor']):
        return 'signature-mismatch'
    if any(s in text for s in ['expected','illegal start','reached end','not a statement','unclosed']):
        return 'syntax'
    return 'other'


def screen_round(exp,config,g,directory,prior=()):
    adapter=JavaMavenAdapter(exp,config)
    if g['method']=='ktester':
        trees=sorted(directory.glob('*/test_classes'))
        sources=sorted(trees[0].glob('*.java')) if trees else sorted((directory/'sources').rglob('*.java'))
    else:sources=sorted((directory/'sources').rglob('*.java'))
    def validate(pair):
        number,path=pair
        record = adapter.screen_file(g['name'],directory.name,path,config['screen']['stable_runs'],number)
        stage = 'screen' if read_json(exp/'state/screen.json', {}).get('status') == 'running' else 'generate'
        log(exp, f'{stage}: {g["name"]} {directory.name} screened {path.name}', config)
        return record
    with ThreadPoolExecutor(max_workers=config['runner']['workers']['build']) as pool:
        records=list(pool.map(validate,enumerate(sources)))
    seen={r['body_hash'] for r in prior};kept=list(prior);stable=duplicate=0
    failures=[]
    for record in records:
        if not record.get('compile') or record['compile']['exit_code']:
            evidence=record.get('compile') or record['parse']
            diagnostic=(exp/evidence['log']).read_text(errors='replace')
            failures.append(dict(file=record['file'],category=classification(diagnostic),diagnostic=diagnostic[:4000]))
        for row in record['methods']:
            if row['state']!='pass':continue
            stable+=1
            if row['body_hash'] in seen:
                duplicate+=1;row['duplicate']=True;continue
            seen.add(row['body_hash']);kept.append(row)
    ndev=len(read_csv(exp/'dev/dev-denominator.csv'))
    funnel=dict(group=g['name'],round=directory.name,files=len(sources),compiled=sum(bool(r['compile']) and r['compile']['exit_code']==0 for r in records),methods=sum(len(r['methods']) for r in records),stable_3_of_3=stable,duplicates=duplicate,kept_this_round=stable-duplicate,total_kept=len(kept),developer_denominator=ndev,ratio_to_dev=len(kept)/ndev if ndev else None,per_prompt={})
    for tech in ['KTester'] if g['method']=='ktester' else ['ZSL','FSL','CoT','ToT','GToT']:
        selected=[r for r in records if g['method']=='ktester' or Path(r['file']).parent.name==tech]
        funnel['per_prompt'][tech]=dict(files=len(selected),compiled=sum(bool(r['compile']) and r['compile']['exit_code']==0 for r in selected),methods=sum(len(r['methods']) for r in selected),stable=sum(m['state']=='pass' for r in selected for m in r['methods']),duplicates=sum(m.get('duplicate',False) for r in selected for m in r['methods']),kept=sum(m['state']=='pass' and not m.get('duplicate',False) for r in selected for m in r['methods']))
    write_json(directory/'funnel.json',funnel)
    atomic_text(directory/'funnel.md','# Generation funnel\n\n```json\n'+json.dumps(funnel,indent=2)+'\n```\n')
    write_json(exp/f'results/{g["name"]}-{directory.name}-compile-failures.json',failures)
    return kept,funnel


def run(exp,config):
    adapter=JavaMavenAdapter(exp,config);adapter.setup_runner()
    frozen={g['name']:[frozen_method(r) for r in read_csv(exp/denominator_file(g,i))] for i,g in enumerate(config['dev']['groups'])}
    from ..dev_results import imported_groups
    frozen.update({name: item['frozen'] for name, item in imported_groups(exp, config).items()})
    validate_derived_subsets(config['dev']['groups'],frozen)
    first_dev=config['dev']['groups'][0]['name']
    for g in config['generators']:
        kept=[]
        for directory in rounds(exp/'generated'/g['name']):
            kept,_=screen_round(exp,config,g,directory,kept)
        if config['screen']['matching']=='subsample-to-dev':
            kept=subsample(kept,len(frozen[first_dev]),g['name'])
        write_csv(exp/f'results/llm-{g["name"]}-denominator.csv',[{k:r[k] for k in FIELDS} for r in kept],FIELDS)
        frozen[g['name']]=[{k:r[k] for k in ['class','method','kind','file','id']} for r in kept]
    write_json(exp/'results/frozen-denominators.json',frozen)
    sizes={g:len(rows) for g,rows in frozen.items()}
    write_json(exp/'results/denominators.json',sizes)
    return 'Frozen denominators: '+', '.join(f'{g}={n}' for g,n in sizes.items())
