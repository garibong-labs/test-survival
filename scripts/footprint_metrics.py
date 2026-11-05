#!/usr/bin/env python3
"""Read-only footprint and timing report for saved fixture runs."""
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import sys

from test_survival.io import read_json
from test_survival.work import footprint

ROOT=Path('/tmp/footprint-e2e').resolve()


def seconds(record):
    return round((datetime.fromisoformat(record['finished'])-datetime.fromisoformat(record['started'])).total_seconds(),3) if record.get('finished') and record.get('started') else None


def metrics(label):
    saved=ROOT/label;experiments=saved/'experiments'
    result=dict(total=footprint(experiments),experiments={},timings=read_json(saved/'timings.json'),stages={},peaks={})
    for exp in sorted(experiments.iterdir()):
        result['experiments'][exp.name]=footprint(exp)
        for record in (exp/'state').glob('*.json'):
            if record.stem in ('prepare','scope','generate','screen','control','survive','cover','report',
                               'policy-pools','policy-survive','policy-cover','policy-report','policy-pool-dev-scope-2'):
                elapsed=seconds(read_json(record,{}))
                if elapsed is not None:result['stages'][exp.name+'/'+record.stem]=elapsed
        phase=read_json(exp/'results/final-summary.json',{}).get('phase_seconds',{})
        for stage,elapsed in phase.items():result['stages'][exp.name+'/'+stage]=elapsed
    interruption=read_json(saved/'interruption.json')
    if interruption:
        result['interruption']=interruption
        prior=read_json(saved/'interrupted-state/policy-survive.json',{})
        duration=seconds(prior)
        result['interrupted_stage_seconds']=duration
        if duration is not None:result['stages']['policy/policy-survive']+=duration
    for sample in read_json(saved/'samples.json'):
        total=dict(checkouts=0,files=0,bytes=0)
        for name,values in sample['experiments'].items():
            for key in total:total[key]+=values[key]
            for stage in values['stages']:
                if stage=='run':continue
                peak=result['peaks'].setdefault(name+'/'+stage,dict(checkouts=0,files=0,bytes=0))
                for key in total:peak[key]=max(peak[key],values[key])
        peak=result.setdefault('combined_peak',dict(checkouts=0,files=0,bytes=0))
        for key in total:peak[key]=max(peak[key],total[key])
    retained=saved_failure_files=savings=saved_success_files=0
    for path in experiments.rglob('results/work-cleanup.json'):
        data=read_json(path);exp=path.parents[1]
        savings+=data['removed_bytes'];saved_success_files+=data['removed_files']
        for relative in data['retained_failures']:
            actual=footprint(exp/relative)
            retained+=actual['bytes'];saved_failure_files+=actual['files']
    caches=[p for p in experiments.rglob('compile-cache') if p.is_dir() and p.parent.name=='work']
    result['compile_cache']=dict(files=sum(footprint(p)['files'] for p in caches),bytes=sum(footprint(p)['bytes'] for p in caches))
    result['successful_scratch_removed']=dict(files=saved_success_files,bytes=savings)
    result['retained_failure_scratch']=dict(files=saved_failure_files,bytes=retained)
    # Checkouts are measured separately from the cumulative scratch removal metric.
    result['remaining_checkouts']=[str(p.relative_to(experiments)) for p in experiments.rglob('checkouts') if p.parent.name=='work']
    return result


if __name__=='__main__':
    report={label:metrics(label) for label in sys.argv[1:]}
    (ROOT/'metrics.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
