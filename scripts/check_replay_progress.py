"""Compare only the contiguous measurements made by the current runner version."""
import argparse
import json
from pathlib import Path

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import load
from test_survival.io import read_csv,read_json,write_json

parser=argparse.ArgumentParser()
parser.add_argument('experiment',type=Path)
parser.add_argument('reference',type=Path)
parser.add_argument('--out',type=Path)
args=parser.parse_args()
exp,ref=args.experiment,args.reference
frozen=read_json(exp/'results/frozen-denominators.json')
provenance=JavaMavenAdapter(exp,load(exp)).provenance()
results=[]
for group,rows in frozen.items():
    alive={r['id'] for r in rows};expected=set(alive);through=0;differences=[]
    for commit in read_csv(exp/'results/commits.csv'):
        index=int(commit['index'])
        actual=read_json(exp/f'results/raw/{index}-{group}.json')
        if actual is None or actual.get('runner_sha256')!=provenance:break
        reference=read_json(ref/f'results/raw/{index}-{group}.json')
        lost={r['id']:r for r in actual['methods'] if r['state']!='pass' and r['id'] in alive}
        expected_lost={r['id']:r for r in reference['methods'] if r['state']!='pass' and r['id'] in expected}
        kinds=[{'id':ident,'actual':lost[ident]['state'],'reference':expected_lost[ident]['state']} for ident in lost.keys()&expected_lost.keys() if lost[ident]['state']!=expected_lost[ident]['state'] or lost[ident]['flaky']!=expected_lost[ident]['flaky']]
        if lost.keys()!=expected_lost.keys() or kinds:
            differences.append({'index':index,'extra_loss':sorted(lost.keys()-expected_lost.keys()),'missing_loss':sorted(expected_lost.keys()-lost.keys()),'first_failure_differences':kinds})
        alive-=lost.keys();expected-=expected_lost.keys();through=index
    results.append({'group':group,'through_index':through,'alive':len(alive),'reference_alive':len(expected),'matches_reference':not differences,'differences':differences})
if args.out:write_json(args.out,results)
print(json.dumps(results,indent=2))
