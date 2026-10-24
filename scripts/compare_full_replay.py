"""Step-6 comparisons, first-failure classification, and raw ordering audit."""
import argparse
import csv
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

import yaml
from test_survival.config import load, canonical_hash
from test_survival.adapters.java_maven import JavaMavenAdapter

FILES = ['table1-survival-by-event.md','table2-breaking-commits.md',
         'table3-changed-line-coverage.md','table4-exposure-vs-break.md',
         'coverage-t.md','survival.csv','first-failures.csv','changed-lines.csv',
         'coverage-commits.csv','consistency.md']
PORT = re.compile(r'\b(?:BindException|ConnectException|ConnectTimeoutException|EADDRINUSE|ECONNREFUSED)\b|address\s+(?:already\s+)?in\s+use|(?:failed|unable|cannot)\s+to\s+(?:bind|connect)\b|\b(?:bind|connect|connection)\s+(?:error|failed|failure|refused|timed\s+out|reset|aborted)\b', re.I)


def read_json(path):
    return json.loads(path.read_text())


def csvrows(path):
    with path.open(newline='') as stream:
        return list(csv.DictReader(stream))


def row_key(row):
    return tuple(sorted(row.items()))


def md(value):
    return str(value).replace('|','\\|').replace('\n','<br>')


def normalized(path):
    return '\n'.join(line.rstrip() for line in path.read_text().splitlines())


def compare_file(replay, reference, name):
    a,b=replay/'results'/name,reference/'results'/name
    if not a.exists() or not b.exists():
        return dict(file=name,status='missing',missing=[str(p) for p in [a,b] if not p.exists()])
    if name=='first-failures.csv':
        x,y=sorted(map(row_key,csvrows(a))),sorted(map(row_key,csvrows(b)))
    elif name=='consistency.md':
        x,y=[json.loads(re.search(r'```json\s*\n(.*?)\n```',p.read_text(),re.S)[1]) for p in [a,b]]
    else:
        x,y=normalized(a),normalized(b)
    result=dict(file=name,status='identical' if x==y else 'differs')
    if x!=y:
        left,right=normalized(a).splitlines(),normalized(b).splitlines()
        if name=='first-failures.csv':
            ar,br=csvrows(a),csvrows(b)
            aset,bset=set(map(row_key,ar)),set(map(row_key,br))
            ai=next((i for i,r in enumerate(ar,2) if row_key(r) not in bset),None)
            bi=next((i for i,r in enumerate(br,2) if row_key(r) not in aset),None)
            result.update(first_differing_line=f'replay {ai}; reference {bi}',replay_line=ai,reference_line=bi,
                          replay=left[ai-1] if ai else '<no replay-only row>',reference=right[bi-1] if bi else '<no reference-only row>')
            return result
        for i in range(max(len(left),len(right))):
            actual=left[i] if i<len(left) else '<EOF>'
            expected=right[i] if i<len(right) else '<EOF>'
            if actual!=expected:
                result.update(first_differing_line=i+1,replay=actual,reference=expected)
                break
    return result


def failure_differences(replay,reference):
    a,b=csvrows(replay/'results/first-failures.csv'),csvrows(reference/'results/first-failures.csv')
    sets=[{row_key(r) for r in rows} for rows in [a,b]]
    differences=[]
    @lru_cache(maxsize=8)
    def raw(path):
        return read_json(path)
    for side,root,rows,other in [('replay',replay,a,sets[1]),('reference',reference,b,sets[0])]:
        for line,row in enumerate(rows,2):
            if row_key(row) in other:continue
            path=root/f"results/raw/{row['index']}-{row['group']}.json"
            method=next((m for m in raw(path)['methods'] if m['id']==row['id']),None)
            messages={key:method.get(key,'') for key in ['message','retry_message']} if method else {}
            evidence=[]
            for key,message in messages.items():
                match=PORT.search(message)
                if match:evidence.append(dict(field=key,matched=match.group(),message=message))
            differences.append(dict(side=side,csv_line=line,row=row,classification='port-collision' if evidence else 'unclassified',raw=str(path),messages=messages,evidence=evidence))
    return sorted(differences,key=lambda r:(int(r['row']['index']),r['row']['group'],r['row']['id'],r['side']))


def order_audit(replay,reference):
    details=[];compared=0;missing=[]
    for file in sorted((reference/'results/raw').glob('*.json')):
        other=replay/'results/raw'/file.name
        if not other.exists():missing.append(file.name);continue
        a,b=read_json(other),read_json(file);compared+=1
        def selectors(record, all_runs=False):
            result=[]
            for part in record['files']:
                launches=part['runs'] if all_runs else part['runs'][:1]
                result.append((part['file'],[[run['command'][i+1] for i,v in enumerate(run['command'][:-1]) if v=='--select-method'] for run in launches]))
            return result
        checks=dict(file_order=[f['file'] for f in a['files']]==[f['file'] for f in b['files']],
                    method_order=[m['id'] for m in a['methods']]==[m['id'] for m in b['methods']],
                    requested_id_order=a['requested_ids']==b['requested_ids'],
                    initial_selector_order=selectors(a)==selectors(b),
                    all_launch_order_including_retries=selectors(a,True)==selectors(b,True))
        if not all(checks.values()):details.append(dict(index=a['index'],group=a['group'],**checks))
    phases={p.name:{k:v for k,v in read_json(p).items() if k in ['workers','resume_workers','jobs']} for p in sorted((reference/'results').glob('phase-a*.json')) if '-oom' not in p.name}
    initial_equal=not missing and all(all(r[k] for k in ['file_order','method_order','requested_id_order','initial_selector_order']) for r in details)
    return dict(compared_records=compared,missing=missing,initial_per_commit_orders_equal=initial_equal,all_per_commit_orders_equal=not details and not missing,differences=details,reference_phase_workers=phases,
                cross_commit_timing='Raw records contain durations, not a complete shared execution timeline; they do not establish identical concurrent interleaving. Conditional retry launches can differ when outcomes differ.')


def comparison(replay,reference):
    config=load(replay)
    failures=failure_differences(replay,reference)
    curve=csvrows(replay/'results/survival.csv')
    fingerprints={g:canonical_hash({'methods':[(r['id'],hashlib.sha256((replay/r['file']).read_bytes()).hexdigest()) for r in methods], 'survival':config['survival'],'runner':config['runner'],'repeats':1}) for g,methods in read_json(replay/'results/frozen-denominators.json').items()}
    provenance=JavaMavenAdapter(replay,config).provenance()
    invalid=[];reused=[];raw_count=0
    for path in (replay/'results/raw').glob('*.json'):
        raw=read_json(path);raw_count+=1
        if raw['input_sha256']!=fingerprints[raw['group']] or raw['runner_sha256']!=provenance:invalid.append(path.name)
        if 'reused_from' in raw:reused.append(path.name)
    result=dict(replay=str(replay),reference=str(reference),survive_workers=config['runner']['workers']['survive'],
                files=[compare_file(replay,reference,name) for name in FILES],
                differing_first_failure_rows=failures,unclassified=[r for r in failures if r['classification']=='unclassified'],
                ordering=order_audit(replay,reference),final_survivors={k.removesuffix('_alive'):int(v) for k,v in curve[-1].items() if k.endswith('_alive')},
                coverage_indices=len(csvrows(replay/'results/coverage-commits.csv')),
                survival_consistency=read_json(replay/'results/survival-consistency.json'),
                provenance=dict(raw_records=raw_count,all_inputs_and_runner_hashes_match=not invalid,invalid_records=invalid,reused_records=reused,runner_sha256=provenance))
    return result


def failure_table(rows):
    lines=['| Side / CSV line | Index | Group | ID | Kind | Classification | Matching failure text |','|---|---:|---|---|---|---|---|']
    for item in rows:
        r=item['row'];evidence='; '.join(e['field']+': '+e['matched'] for e in item['evidence']) or 'No bind/connect/address-in-use match'
        cells=[md(v) for v in [item['side']+' / '+str(item['csv_line']),r['index'],r['group'],r['id'],r['kind'],item['classification']]]
        cells.append('['+md(evidence)+']('+item['raw']+')')
        lines.append('| '+' | '.join(cells)+' |')
    return '\n'.join(lines) if rows else 'None.'


def markdown(reports):
    lines=['# Acceptance comparison','',
           'Each replay is compared with the read-only hand-run reference. Text comparisons normalize trailing whitespace; first-failure CSVs use unordered row sets; consistency compares its JSON block.',
           '', 'The `port-collision` label follows the requested message rule: a bind/connect error or an address-in-use condition in the original or retry failure message. It does not independently establish that every connection error was caused by a port collision.', '']
    for label,report in reports:
        lines += ['## '+label,'',f"Replay: `{report['replay']}`. Reference: `{report['reference']}`.",'',f"`runner.workers.survive`: **{report['survive_workers']}**.",'',
                  '| Compared file | Result | First differing line |','|---|---|---:|']
        for row in report['files']:
            lines.append('| '+row['file']+' | '+row['status']+' | '+str(row.get('first_differing_line',''))+' |')
        for row in report['files']:
            if row['status']=='differs':
                location=(f"First differing unordered rows: replay CSV line {row['replay_line']}; reference CSV line {row['reference_line']}. All differing rows are listed below." if row['file']=='first-failures.csv' else f"First differing physical line: {row['first_differing_line']}.")
                lines += ['',f"### {row['file']}",'',location,'','Replay:','```text',row['replay'],'```','Reference:','```text',row['reference'],'```']
        lines += ['', '### Run totals', '', '```json',json.dumps({k:report[k] for k in ['final_survivors','coverage_indices','survival_consistency']},indent=2),'```',
                  '',f"Raw-record configuration and runner hashes all match this replay: **{report['provenance']['all_inputs_and_runner_hashes_match']}**. Reused records: {len(report['provenance']['reused_records'])}.",
                  '', '### Execution ordering', '',f"Compared {report['ordering']['compared_records']} raw records. Same per-commit file, method, requested-ID, and initial selector ordering: **{report['ordering']['initial_per_commit_orders_equal']}**. Same complete launch sequence including conditional retries: **{report['ordering']['all_per_commit_orders_equal']}**.",'',report['ordering']['cross_commit_timing'],'',
                  'Reference phase metadata:', '```json',json.dumps(report['ordering']['reference_phase_workers'],indent=2),'```']
        if report['ordering']['differences']:
            lines += ['', 'Ordering differences:', '```json',json.dumps(report['ordering']['differences'],indent=2),'```']
        lines += ['', '### Every differing first-failure row', '',failure_table(report['differing_first_failure_rows']),
                  '', '### Unclassified first-failure rows', '',failure_table(report['unclassified']), '']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--reference',type=Path,required=True)
    parser.add_argument('--replay',action='append',required=True,help='label=path; repeat for both runs')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--json-out',type=Path,required=True)
    args=parser.parse_args();reports=[]
    for item in args.replay:
        label,path=item.split('=',1)
        reports.append((label,comparison(Path(path),args.reference)))
    args.json_out.parent.mkdir(parents=True,exist_ok=True)
    args.json_out.write_text(json.dumps(dict(reports),indent=2)+'\n')
    args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(markdown(reports)+'\nFull comparison data and failure messages: ['+args.json_out.name+']('+str(args.json_out.resolve())+').\n')
    for label,result in reports:
        print(json.dumps(dict(label=label,survive_workers=result['survive_workers'],differing_files=sum(r['status']!='identical' for r in result['files']),differing_first_failure_rows=len(result['differing_first_failure_rows']),unclassified=len(result['unclassified']))))


if __name__=='__main__':main()
