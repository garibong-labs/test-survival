"""Union per-test measurements, assembled into changed-scope-line coverage."""
from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_csv, read_json, write_csv, write_json
from .pools import coverage
from .segments import key


def run(b, step):
    adapter = JavaMavenAdapter(b.exp, b.execution_config)
    policy_names = [p['name'] for p in b.options['set']]
    alive = {p:read_json(b.exp/f'results/policy/{p}/alive.json') for p in ['none',*policy_names]}
    rows = {p:[] for p in policy_names}
    scope = {r['file'] for r in b.scope['classes']}
    ready = False
    for change_row in read_csv(b.path/'results/changed-lines.csv'):
        index = int(change_row['index'])
        if not change_row.get('scope_executable') or int(change_row['scope_executable']) == 0 or not b.evaluable(index):
            continue
        change = read_json(b.path/f'results/changes/{index}.json')
        if change is None:
            raise ValueError('Missing changed-line image at '+str(index))
        changed = {(f,int(n)) for f,ns in change['post_image'].items() if f in scope for n in ns}
        tests = {key(t):t for p in alive for g in b.groups for t in alive[p][str(index)][g]}
        path = b.exp/f'results/policy/coverage/{index}.json'
        def measure():
            nonlocal ready
            if not ready:
                if adapter.build(0,b.anchor['sha'])['build'] != 'ok':
                    raise RuntimeError('Coverage anchor build failed')
                adapter.setup_runner(); ready = True
            if adapter.build(index,b.by_index[index]['sha'])['build'] != 'ok':
                raise RuntimeError('Coverage build differs from baseline')
            raw = coverage(adapter,index,list(tests.values()),str(index))
            write_json(path,raw)
        step('coverage-'+str(index),measure,outputs=[path])
        raw = read_json(path)
        observed = {t['file_hash']+'|'+t['class']+'#'+t['method']:t for t in raw['methods']}
        if set(observed) != set(tests):
            raise ValueError('Coverage union is incomplete')
        universe_path = next((b.path/f'results/coverage/{index}-{g}-lines.csv' for g in b.frozen
                              if (b.path/f'results/coverage/{index}-{g}-lines.csv').exists()), None)
        executable = changed & {(r['file'],int(r['line'])) for r in read_csv(universe_path)} if universe_path else changed
        if len(executable) != int(change_row['scope_executable']):
            raise ValueError('Changed executable scope lines differ from baseline at '+str(index))
        for p in policy_names:
            row = {k:change_row[k] for k in ('index','sha','date','subject')}
            row['scope_executable'] = len(executable)
            for g in b.groups:
                hits = {tuple(line) for t in alive[p][str(index)][g] for line in observed[key(t)]['lines']}
                row[g+'_covered'] = len(hits & executable)
                row[g+'_percent'] = round(100*row[g+'_covered']/len(executable),3) if executable else ''
            rows[p].append(row)
    for p in policy_names:
        output = b.exp/'results/policy'/p
        fields = ['index','sha','date','subject','scope_executable',*[g+s for g in b.groups for s in ('_covered','_percent')]]
        write_csv(output/'coverage-commits.csv',rows[p],fields)
        total = dict(executable=sum(r['scope_executable'] for r in rows[p]),groups={g:sum(r[g+'_covered'] for r in rows[p]) for g in b.groups})
        write_json(output/'coverage-total.json',total)
