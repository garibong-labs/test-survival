import json
from datetime import datetime
from ..io import read_json,read_csv,atomic_text,write_json,now


def build(exp,config):
    groups=['dev',*[g['name'] for g in config['generators']]]
    labels={'dev':'Dev',**{g['name']:g['display'] for g in config['generators']}}
    survival=read_json(exp/'results/survival-consistency.json');coverage=read_json(exp/'results/coverage-consistency.json')
    counts=read_json(exp/'results/denominators.json');curves=read_csv(exp/'results/survival.csv')
    final={g:int(curves[-1][g+'_alive']) for g in groups}
    text='# Consistency checks\n\n```json\n'+json.dumps(survival,indent=2)+'\n```\n'
    text+=f'\nCoverage: identical executable line sets across all {len(groups)} groups at {len(coverage["identical_executable_line_sets"])} indices. Required indices: {coverage["coverage_indices"]}. Missing measurements: {len(coverage["missing"])} (enumerated in coverage-consistency.json).\n'
    number={3:'Three',4:'Four',5:'Five'}.get(len(groups),str(len(groups)))
    text+='\n## '+number+'-group checks\n\n| Check | '+' | '.join(labels[g] for g in groups)+' |\n|---|'+'---:|'*len(groups)+'\n'
    for label,values in [('Fixed denominator',[counts[g] for g in groups]),('Final survivors',[final[g] for g in groups]),('Survival records',[len(list((exp/'results/raw').glob(f'*-{g}.json'))) for g in groups]),('Coverage records',[len(list((exp/'results/raw-coverage').glob(f'*-{g}.json'))) for g in groups]),('Matching executable universes',[len(coverage['identical_executable_line_sets'])]*len(groups))]:
        text+='| '+label+' | '+' | '.join(map(str,values))+' |\n'
    atomic_text(exp/'results/consistency.md',text)
    timings={}
    for stage in ['prepare','scope','generate','screen','control','survive','cover']:
        state=read_json(exp/f'state/{stage}.json',{})
        if state.get('started') and state.get('finished'):
            timings[stage]=round((datetime.fromisoformat(state['finished'])-datetime.fromisoformat(state['started'])).total_seconds(),3)
    rounds={g:read_json(exp/f'generated/{g}/rounds.json',{}) for g in groups if g!='dev'}
    verification=dict(nonincreasing=survival['nonincreasing'],raw_count_matches=survival['raw_count_matches'],missing_survival_records=len(survival['missing_records']),missing_coverage_records=len(coverage['missing']))
    summary=dict(status='completed' if not survival['missing_records'] and not coverage['missing'] else 'completed-with-unavailable-measurements',finished=now(),denominators=counts,final_survivors=final,model_rounds=rounds,phase_seconds=timings,final_verification=verification,survival_consistency=survival,coverage_consistency=coverage,pushed=False)
    write_json(exp/'results/final-summary.json',summary)
    return summary
