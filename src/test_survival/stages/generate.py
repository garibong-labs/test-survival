import shutil
from datetime import datetime,timezone
from ..config import stage_hash,legacy_stage_hash
from ..generators.ktester_tool import KTesterTool, verified_archives
from ..generators.ouedraogo_native import OuedraogoNative
from ..io import read_csv,read_json,write_json
from ..pipeline import log
from ..rules.stop import decision
from .screen import screen_round


def run(exp,config,group=None,force=False):
    from ..adapters.java_maven import JavaMavenAdapter
    JavaMavenAdapter(exp,config).setup_runner()
    ndev=len(read_csv(exp/'dev/dev-denominator.csv'))
    saved=read_json(exp/'state/generate.json',{})
    stage_current=saved.get('status')=='completed' and saved.get('config_hash')==stage_hash(config,'generate')
    summaries=[]
    for g in config['generators']:
        if group and group!=g['name']:continue
        if force:
            source=exp/'generated'/g['name']
            if source.exists():
                archive=exp/'work/generation-history'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')/g['name']
                archive.parent.mkdir(parents=True,exist_ok=True);shutil.move(source,archive)
                work = exp/'work/ktester'/g['name']
                if g['method'] == 'ktester' and work.exists():
                    work.rename(archive.parent/('worker-'+g['name']))
        digest=stage_hash(config,'generate',g['name'])
        path=exp/'generated'/g['name']/'rounds.json'
        old=read_json(path,{})
        trusted=stage_current or saved.get('group_hashes',{}).get(g['name'])==digest
        verified = g['method'] != 'ktester' or verified_archives(path.parent, g['rounds']['max'])
        if verified and (trusted or old.get('config_hash') in (digest, legacy_stage_hash(config, 'generate', g['name']))) and old.get('stopped'):
            summaries.append(g['name']+': already generated');continue
        history=[];entries=[];kept=[];round_no=1
        while True:
            if g['method']=='ouedraogo':generator=OuedraogoNative(exp,config,g)
            else:
                generator=KTesterTool(exp,config,g)
            result=generator.generate(round_no)
            label=f'round{round_no}'
            directory=exp/'generated'/g['name']/label
            kept,funnel=screen_round(exp,config,g,directory,kept)
            history.append(funnel['compiled'])
            action=decision(g['rounds'],round_no,history,len(kept),ndev)
            entries.append(dict(round=round_no,directory=label,funnel=funnel,model_calls=len(result.calls)))
            write_json(path,dict(config_hash=digest,rounds=entries,stopped=action if action!='next' else None))
            post = funnel.get('post_process')
            repair = f"; post-process {post['visited']}/{post['total']} visited, {post['skipped']} skipped" if post else ''
            log(exp,f'generate: {g["name"]} {label}: {funnel["compiled"]} compiled files; {len(kept)} kept{repair}; {action}',config)
            if action=='next':round_no+=1
            else:break
        summaries.append(g['name']+': '+action)
    return '; '.join(summaries)
