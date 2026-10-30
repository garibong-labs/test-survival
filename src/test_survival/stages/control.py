from ..config import execution_groups
from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json, write_json, atomic_text


def run(exp,config):
    from ..dev_results import imported_for, imported_record
    provenance, imported = imported_for(exp, config, 'control')
    adapter=JavaMavenAdapter(exp,config)
    frozen=execution_groups(config, read_json(exp/'results/frozen-denominators.json'))
    if set(frozen) - imported.keys():
        adapter.setup_runner()
    records={}
    anchor_sha = read_json(exp/'results/anchor.json')['sha']
    for group,rows in frozen.items():
        if group in imported:
            raw = imported_record(exp, f'results/control/0-{group}.json',
                                  sha=anchor_sha,
                                  method_ids=[r['id'] for r in rows], provenance=provenance)
        else:
            raw = adapter.run_group(0,group,rows,'control')
        records[group]=dict(requested=len(rows),passed=sum(r['state']=='pass' for r in raw['methods']),failures=[r for r in raw['methods'] if r['state']!='pass'])
        from ..panel import notify
        notify(exp)
    write_json(exp/'results/control-at-t.json',records)
    atomic_text(exp/'results/control-at-t.md','# Control at t\n\n'+'\n'.join(f'- {g}: {r["passed"]}/{r["requested"]} passed.' for g,r in records.items())+'\n')
    if any(r['failures'] for r in records.values()):
        raise RuntimeError('Frozen methods failed the control at t')
    return 'All frozen methods passed at the anchor'
