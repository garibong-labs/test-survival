"""Read unified and historical experiment layouts; emit a portable HTML file."""
import json
from pathlib import Path
import yaml

from ..io import read_csv,read_json,atomic_text
from ..config import expand_path


def coverage(path,groups):
    file=path/'results/changed-lines.csv'
    if not file.exists():return {g:None for g in groups}
    rows=read_csv(file);result={}
    for g in groups:
        covered=executable=0;measured=False
        for r in rows:
            c=r.get('all_'+g+'_covered',r.get(g+'_covered',''))
            n=r.get('all_executable',r.get('all_executable_changed_lines',r.get('executable','')))
            if c!='' and n!='':covered+=int(c);executable+=int(n);measured=True
        result[g]={'covered':covered,'executable':executable} if measured else None
    return result


def unified(path):
    rows=read_csv(path/'results/survival.csv');denoms=read_json(path/'results/denominators.json')
    groups={g:[{'x':int(r['index']),'alive':int(r[g+'_alive'])} for r in rows] for g in denoms}
    config=yaml.safe_load((path/'experiment.yaml').read_text()) if (path/'experiment.yaml').exists() else {}
    anchor=read_json(path/'results/anchor.json',{})
    commits=read_csv(path/'results/commits.csv') if (path/'results/commits.csv').exists() else rows[1:]
    return dict(project=Path(config.get('project',{}).get('repo',path.parents[1])).name,language=config.get('project',{}).get('language','java'),anchor=config.get('anchor',anchor.get('tag',rows[0]['sha'])),commit_count=len(commits),denominators=denoms,groups=groups,coverage=coverage(path,groups))


def per_group(path):
    groups={};denoms={};anchor='unknown';maximum=0
    for file in sorted((path/'results').glob('*-survival.csv')):
        group=file.name.removesuffix('-survival.csv');rows=read_csv(file)
        if not rows:continue
        if 'survived' in rows[0]:
            values=[{'x':int(r['index']),'alive':int(r['survived'])} for r in rows]
            denoms[group]=int(rows[0]['denominator']);anchor=rows[0].get('tag') or rows[0].get('sha7','unknown')
        elif 'alive_count' in rows[0]:
            values=[{'x':int(r['index']),'alive':int(r['alive_count'])} for r in rows]
            denoms[group]=int(rows[0]['alive_count']);anchor=rows[0]['sha'][:12]
        else:raise ValueError(f'Unsupported historical survival columns: {file}')
        groups[group]=values;maximum=max(maximum,max(r['x'] for r in values))
    if not groups:raise ValueError('No per-group survival files: '+str(path))
    return dict(project=path.parents[1].name,language='java',anchor=anchor,commit_count=maximum,denominators=denoms,groups=groups,coverage=coverage(path,groups))


def testpilot_ss(path):
    rows=read_csv(path/'results/survival-by-commit.csv')
    mapping={'dev':'dev_cum_subtests','llm':'llm_cum'}
    groups={g:[{'x':int(r['n']),'alive':int(r[col])} for r in rows] for g,col in mapping.items()}
    denoms={g:int(rows[0][col]) for g,col in mapping.items()}
    return dict(project=path.parents[1].name,language='javascript',anchor=rows[0]['tags'] or rows[0]['sha7'],commit_count=max(int(r['n']) for r in rows),denominators=denoms,groups=groups,coverage=coverage(path,groups))


READERS={'unified':unified,'per-group':per_group,'testpilot-ss':testpilot_ss}


def build(experiments,out):
    experiments,out=expand_path(experiments, 'experiments path'),expand_path(out, 'dashboard output')
    config=yaml.safe_load(experiments.read_text())
    if not isinstance(config,dict) or set(config)!={'experiments'} or not isinstance(config['experiments'],list):raise ValueError('experiments.yaml must contain an experiments list')
    data=[]
    for index, item in enumerate(config['experiments']):
        if set(item)-{'path','title','layout'}:raise ValueError('Unknown experiment entry key')
        if item['layout'] not in READERS:raise ValueError('layout must be unified, per-group, or testpilot-ss')
        path=expand_path(item['path'], f'experiments[{index}].path');path=path if path.is_absolute() else experiments.parent/path
        result=READERS[item['layout']](path)
        result['title']=item.get('title',result['project']);data.append(result)
    payload=json.dumps(data,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    template=Path(__file__).parent/'template/index.html'
    atomic_text(out/'index.html',template.read_text().replace('__EXPERIMENT_DATA__',payload))
    print(out/'index.html')
