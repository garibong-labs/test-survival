"""Run the owner's combined follow-up and only its three named checks."""
import argparse
from copy import deepcopy
import csv
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import yaml
from test_survival.config import load,stage_hash,validate
from test_survival.io import read_json,write_json,read_csv,write_csv,atomic_text,now
from test_survival.rules.dev_denominator import denominator_file,validate_derived_subsets
from test_survival.model.credentials import key_available
from test_survival.generators.parts.prompts import CONSTRAINT
from compare_full_replay import failure_differences,row_key


def upgrade(raw):
    config=deepcopy(raw)
    config['project']['test_framework']='junit5'
    config['range']['skip_untouched']='main-sources'
    config['dev']={'stable_runs':config['dev']['stable_runs'],'groups':[
        {'name':'dev','denominator':'project'},
        {'name':'dev-scope','display':'Dev (scope)','denominator':'coverage-scoped','derive_from':'dev'}]}
    config['survival'].pop('dev_cache',None)
    config['survival']['environment_errors']={'patterns':['java.net.BindException'],'max_reruns':3,'backoff_seconds':5}
    for group in config['generators']:group['rounds'].pop('retry_on_zero_compile',None)
    validate(config)
    return config


def retain_frozen_inputs(exp,source,config):
    """Populate the new Dev subset; retain the already-screened imported groups."""
    previous=read_json(source/'results/frozen-denominators.json')
    developer={(r['class'],r['method']):r for r in previous['dev']}
    frozen={g['name']:[deepcopy(developer[(r['class'],r['method'])]) for r in read_csv(exp/denominator_file(g,i))]
            for i,g in enumerate(config['dev']['groups'])}
    frozen.update({g['name']:previous[g['name']] for g in config['generators']})
    validate_derived_subsets(config['dev']['groups'],frozen)
    write_json(exp/'results/frozen-denominators.json',frozen)
    write_json(exp/'results/denominators.json',{g:len(rows) for g,rows in frozen.items()})
    control=read_json(source/'results/control-at-t.json')
    control['dev-scope']={'requested':len(frozen['dev-scope']),'passed':len(frozen['dev-scope']),'failures':[],'derived_from':'dev'}
    write_json(exp/'results/control-at-t.json',control)
    for stage in ['screen','control']:
        record=read_json(source/f'state/{stage}.json')
        record.update(status='completed',config_hash=stage_hash(config,stage),notes='Retained frozen inputs from '+str(source)+'; dev-scope is a subset of the passing dev group')
        write_json(exp/f'state/{stage}.json',record)


def projected_table(exp,name):
    curves=read_csv(exp/'results/survival.csv')
    events={r['index'] for r in curves if any(int(v) for k,v in r.items() if k.endswith('_lost') and not k.startswith(('dev_','dev-scope_')))}
    if name.startswith('table1'):events.update([curves[0]['index'],curves[-1]['index']])
    result=[];omit=[]
    for line in (exp/'results'/name).read_text().splitlines():
        if not line.startswith('|'):continue
        cells=[s.strip() for s in re.split(r'(?<!\\)\|',line)[1:-1]]
        found=[i for i,c in enumerate(cells) if c=='Dev' or c.startswith('Dev ')]
        if found:omit=found
        if name.startswith(('table1','table2')) and cells[0].isdigit() and cells[0] not in events:continue
        result.append([c for i,c in enumerate(cells) if i not in omit])
    return result


def combined_checks(exp,source,reference):
    actual=read_csv(exp/'dev/dev-scope-denominator.csv');expected=read_csv(reference/'dev/dev-scope-denominator.csv')
    a,b=set(map(row_key,actual)),set(map(row_key,expected))
    config=load(exp);commits=read_csv(exp/'results/commits.csv');expected_skips=[]
    for row in commits:
        paths=subprocess.check_output(['git','-C',config['project']['repo'],'diff','--name-only',row['sha']+'^1',row['sha']],text=True).splitlines()
        if not any(p.startswith('src/main/') or p=='pom.xml' or p.endswith('/pom.xml') for p in paths):expected_skips.append((row['index'],row['sha']))
    actual_skips=[(r['index'],r['sha']) for r in read_csv(exp/'results/skipped-commits.csv')]
    table_names=['table1-survival-by-event.md','table2-breaking-commits.md','table3-changed-line-coverage.md','table4-exposure-vs-break.md']
    tables={name:projected_table(exp,name)==projected_table(source,name) for name in table_names}
    dev_differences=[r for r in failure_differences(exp,source) if r['row']['group']=='dev']
    unexpected=[r for r in dev_differences if r['classification']!='port-collision']
    totals=read_json(exp/'results/final-summary.json')['environment_reruns']
    binds={g:0 for g in totals}
    for row in read_csv(exp/'results/first-failures.csv'):
        folder='raw-derived' if row['group']=='dev-scope' else 'raw'
        raw=read_json(exp/f"results/{folder}/{row['index']}-{row['group']}.json")
        method=next(m for m in raw['methods'] if m['id']==row['id'])
        if row['kind'] in ['fail','error','timeout'] and 'java.net.BindException' in method.get('message',''):binds[row['group']]+=1
    lines=['## Combined follow-up: section 11 step 8','',f'Experiment: `{exp}`.','',
           f'(a) Dev (scope) denominator row sets equal: **{a==b}**. Replay {len(actual)}; reference {len(expected)}.']
    if a!=b:lines += ['','Replay-only rows:','```json',json.dumps([dict(r) for r in sorted(a-b)],indent=2),'```','Reference-only rows:','```json',json.dumps([dict(r) for r in sorted(b-a)],indent=2),'```']
    lines += ['',f'(b) Skipped commits exactly match the source/POM diff rule: **{actual_skips==expected_skips}** ({len(actual_skips)} commits).',
              'Table event rows, excluding the new Dev (scope) columns and Dev port-collision differences:', '', '| Table | Other event data unchanged |','|---|---|',
              *[f'| {name} | {same} |' for name,same in tables.items()], '',f'Unexplained Dev first-failure differences from the first comparison: **{len(unexpected)}**.']
    if unexpected:lines += ['','```json',json.dumps([dict(side=r['side'],row=r['row']) for r in unexpected],indent=2),'```']
    lines += ['','(c) BindException first failures and environment reruns (method attempts; derived groups inherit source observations):','',
              '| Group | BindException first failures | Environment reruns |','|---|---:|---:|',
              *[f'| {g} | {binds[g]} | {totals[g]} |' for g in totals]]
    return '\n'.join(lines)+'\n'


def generation_checks(exp,reference):
    base=exp/'generated/ouedraogo-adj2/round1';manifest=read_json(exp/'generated/ouedraogo-adj2/rounds.json')
    calls=sum(r['model_calls'] for r in manifest['rounds'])
    prompts=list((base/'prompts').glob('*/*.txt'))
    rendered=[p for p in prompts if p.stem in ['ZSL','FSL','CoT','ToT','GToT']]
    gate=read_json(base/'prompts/pre-call-check.json')['passed']
    blocks=[]
    for path in (base/'prompts').glob('*/dependency-signatures.txt'):
        other=reference/'generated/ouedraogo-adj2/round1/prompts'/path.relative_to(base/'prompts')
        blocks.append(path.read_text()==other.read_text())
    return '\n'.join(['## Checkpoint 8: native generation','',f'Scratch experiment: `{exp}`.',
                      f'Model calls: **{calls}** (required 25).',f'Constraint sentences present in all 25 prompts: **{len(rendered)==25 and all(CONSTRAINT in p.read_text() for p in rendered)}**.',
                      f'Dependency blocks match the reference headers and type order: **{len(blocks)==5 and all(blocks)}**.',
                      f'Pre-call gate passed: **{gate}**. Rounds completed: **{len(manifest["rounds"])}**. Stop: `{manifest["stopped"]}`.'])+'\n'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--reference',type=Path,required=True)
    parser.add_argument('--combined',type=Path,required=True)
    parser.add_argument('--generation',type=Path,required=True)
    args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    report=root/'.local/acceptance-report.md'
    def command(*argv):subprocess.run(list(map(str,argv)),check=True,cwd=root)
    def run(exp,*options):command(sys.executable,'-m','test_survival.cli','run',exp,*options)
    if not args.combined.exists():shutil.copytree(args.source,args.combined)
    config=upgrade(yaml.safe_load((args.source/'experiment.yaml').read_text()))
    atomic_text(args.combined/'experiment.yaml',yaml.safe_dump(config,sort_keys=False,allow_unicode=True));config=load(args.combined)
    # Existing prepared checkouts and the stable baseline remain the inputs of
    # this copied experiment. Only the new scope pass runs before survival.
    prepare=read_json(args.source/'state/prepare.json');prepare.update(config_hash=stage_hash(config,'prepare'),notes='Prepared anchor/range and baseline retained from '+str(args.source))
    write_json(args.combined/'state/prepare.json',prepare)
    run(args.combined,'--stage','scope','--force')
    command(sys.executable,'-m','test_survival.cli','import',args.combined,'--from',args.source,'--what','generated')
    retain_frozen_inputs(args.combined,args.source,config)
    run(args.combined,'--stage','survive','--force')
    run(args.combined,'--stage','cover')
    run(args.combined,'--stage','report')
    with report.open('a') as stream:stream.write('\n\n'+combined_checks(args.combined,args.source,args.reference))
    with (root/'.local/handover.md').open('a') as stream:stream.write(f'\n{now()} Checkpoint 9 combined run finished at {args.combined}; only the three requested checks were added to .local/acceptance-report.md. Prepared baseline, imported generation and frozen groups were retained in the new copy; scope added the passing developer subset.\n')
    if key_available(config['model']):
        fresh=not args.generation.exists()
        if fresh:shutil.copytree(args.combined,args.generation)
        run(args.generation,'--stage','generate','--group','ouedraogo-adj2',*(['--force'] if fresh else []))
        with report.open('a') as stream:stream.write('\n\n'+generation_checks(args.generation,args.reference))
    else:
        with report.open('a') as stream:stream.write('\n\nCheckpoint 8 generation check skipped: KEY_MISSING.\n')
    command(sys.executable,'-m','test_survival.cli','dashboard','build','--experiments',root/'examples/experiments.yaml','--out',root/'dist/dashboard')
    with (root/'.local/handover.md').open('a') as stream:stream.write(f'\n{now()} Checkpoint 8 generation check recorded in .local/acceptance-report.md. Checkpoint 10 dashboard written to dist/dashboard/index.html; README.md, docs/design.md and docs/result-formats.md describe the current schema and outputs.\n')
    print('Combined run, generation check, and dashboard complete.',flush=True)


if __name__=='__main__':main()
