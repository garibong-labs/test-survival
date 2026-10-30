from ..config import group_names,group_labels
from ..rules.dev_denominator import unmapped_note
from ..stages.survive import raw_folder
import json
from datetime import datetime
from ..io import read_json,read_csv,atomic_text,write_json,now


def build(exp,config):
    groups=group_names(config)
    labels=group_labels(config)
    survival=read_json(exp/'results/survival-consistency.json');coverage=read_json(exp/'results/coverage-consistency.json')
    counts={g:n for g,n in read_json(exp/'results/denominators.json').items() if g in groups};curves=read_csv(exp/'results/survival.csv')
    final={g:int(curves[-1][g+'_alive']) for g in groups}
    raw_paths={g:[p for row in curves[1:] if int(row['index']) not in survival.get('build_fail_indices', []) if (p:=exp/f'results/{raw_folder(g,config)}/{row["index"]}-{g}.json').exists()] for g in groups}
    text='# Consistency checks\n\n```json\n'+json.dumps(survival,indent=2)+'\n```\n'
    excluded=read_json(exp/'results/anchor.json',{}).get('dev_unmapped_excluded',0)
    notes=[unmapped_note(excluded)] if excluded else []
    if notes:text+='\n'+notes[0]+'\n'
    text+=f'\nCoverage: identical executable line sets across all {len(groups)} groups at {len(coverage["identical_executable_line_sets"])} indices. Required indices: {coverage["coverage_indices"]}. Missing measurements: {len(coverage["missing"])} (enumerated in coverage-consistency.json).\n'
    number={3:'Three',4:'Four',5:'Five'}.get(len(groups),str(len(groups)))
    text+='\n## '+number+'-group checks\n\n| Check | '+' | '.join(labels[g] for g in groups)+' |\n|---|'+'---:|'*len(groups)+'\n'
    for label,values in [('Fixed denominator',[counts[g] for g in groups]),('Final survivors',[final[g] for g in groups]),('Survival records',[len(raw_paths[g]) for g in groups]),('Coverage records',[len(list((exp/'results/raw-coverage').glob(f'*-{g}.json'))) for g in groups]),('Matching executable universes',[len(coverage['identical_executable_line_sets'])]*len(groups))]:
        text+='| '+label+' | '+' | '.join(map(str,values))+' |\n'
    atomic_text(exp/'results/consistency.md',text)
    timings={}
    for stage in ['prepare','scope','generate','screen','control','survive','cover']:
        state=read_json(exp/f'state/{stage}.json',{})
        if state.get('started') and state.get('finished'):
            timings[stage]=round((datetime.fromisoformat(state['finished'])-datetime.fromisoformat(state['started'])).total_seconds(),3)
    rounds={g['name']:read_json(exp/f'generated/{g["name"]}/rounds.json',{}) for g in config['generators']}
    environment_reruns={g:sum(m.get('environment_reruns',0) for path in raw_paths[g] for m in read_json(path).get('methods',[])) for g in groups}
    verification=dict(nonincreasing=survival['nonincreasing'],raw_count_matches=survival['raw_count_matches'],missing_survival_records=len(survival['missing_records']),missing_coverage_records=len(coverage['missing']))
    summary=dict(environment_reruns=environment_reruns,environment_rerun_unit='method attempts; derived groups inherit their source observations',status='completed' if not survival['missing_records'] and not coverage['missing'] else 'completed-with-unavailable-measurements',finished=now(),denominators=counts,final_survivors=final,model_rounds=rounds,phase_seconds=timings,final_verification=verification,survival_consistency=survival,coverage_consistency=coverage,pushed=False)
    from ..dev_results import load_provenance
    summary.update(dev_unmapped_excluded=excluded,notes=notes)
    imported = load_provenance(exp)
    if imported:
        summary['imported_dev_results'] = dict(source=imported['source'], groups=sorted(imported['groups']),
                                              imported=imported['imported'], source_sha256=imported['source_sha256'])
    write_json(exp/'results/final-summary.json',summary)
    return summary
