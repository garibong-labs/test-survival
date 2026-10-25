from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json, write_json, atomic_text


def run(exp,config):
    adapter=JavaMavenAdapter(exp,config);adapter.setup_runner()
    frozen=read_json(exp/'results/frozen-denominators.json')
    records={}
    for group,rows in frozen.items():
        raw=adapter.run_group(0,group,rows,'control')
        records[group]=dict(requested=len(rows),passed=sum(r['state']=='pass' for r in raw['methods']),failures=[r for r in raw['methods'] if r['state']!='pass'])
        from ..panel import notify
        notify(exp)
    write_json(exp/'results/control-at-t.json',records)
    atomic_text(exp/'results/control-at-t.md','# Control at t\n\n'+'\n'.join(f'- {g}: {r["passed"]}/{r["requested"]} passed.' for g,r in records.items())+'\n')
    if any(r['failures'] for r in records.values()):
        raise RuntimeError('Frozen methods failed the control at t')
    return 'All frozen methods passed at the anchor'
