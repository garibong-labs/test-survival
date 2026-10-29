"""Independent regeneration pools, using existing generators and screening."""
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
import threading

from ..adapters.java_maven import JavaMavenAdapter
from ..config import canonical_hash, generator_config
from ..io import read_csv, read_json, write_csv, write_json
from ..rules.dev_denominator import denominator, frozen_method
from ..stages.generate import run as generate
from ..stages.screen import screen_round
from ..importer import rounds
from .artifacts import snapshot


def coverage(adapter, index, tests, name, full_suite=False):
    # Filesystem-relative resources require separate suite runs for source snapshots.
    # Every run still launches all of its tests in one JVM, never one JVM per method.
    contexts = defaultdict(list)
    for t in tests:
        contexts[t.get('resource_root', 'dev/src-test-t')].append(t)
    combined = dict(status='evaluated', index=index, methods=[], universe=[], runs=[])
    sha = read_json(adapter.exp/f'results/builds/{index}.json')['sha']
    combined['sha'] = sha
    for number, members in enumerate(contexts.values()):
        label = name+'-'+str(number)
        result = adapter.worker(dict(action='policy-cover', index=index, sha=sha, group='policy-coverage', methods=members,
                                     full_suite=full_suite, output=f'results/policy-coverage/{label}.json', work=f'work/policy-coverage/{label}'))
        combined['methods'].extend(result['methods'])
        combined['universe'].extend(result['universe'])
        combined['runs'].append(result)
    combined['universe'] = sorted({tuple(line) for line in combined['universe']})
    return combined


def build_pool(b, point, dev_target):
    group, index = point['group'], point['index']
    exp = b.exp/f'results/policy/pools/{group}/{index}'
    owner = read_json(b.exp/'state/run.json', {})
    if owner.get('status') != 'running' or not owner.get('run_id'):
        raise RuntimeError('Pool generation requires a registered policy run')
    write_json(exp/'state/run-owner.json', dict(experiment=str(b.exp), run_id=owner['run_id']))
    config = deepcopy(b.execution_config)
    config['anchor'] = point['sha']
    config['screen']['stable_runs'] = 3
    config['dev']['stable_runs'] = 3
    config['runner']['reuse'] = {'checkouts':None, 'classpath_cache':None}
    g = next((g for g in config['generators'] if g['name'] == group), None)
    if g is not None:
        config = generator_config(config, g)
        config['generators'] = [g]
    adapter = JavaMavenAdapter(exp, config)
    write_json(exp/'results/anchor.json', dict(sha=point['sha'], date=b.by_index[index]['date']))
    if adapter.build(0, point['sha'])['build'] != 'ok':
        raise RuntimeError(f'Planned pool build failed: {group}/{index}')
    test_source = adapter.folder(0)/'src/test'
    if test_source.exists() and not (exp/'dev/src-test-t').exists():
        shutil.copytree(test_source, exp/'dev/src-test-t')
    adapter.setup_runner()
    main = adapter.list_production_methods(0)
    tests = adapter.list_test_methods(exp/'dev/src-test-t/java')
    write_json(exp/'results/source-inventory.json', dict(main=main, test=tests))
    scope = [c for c in b.scope['classes'] if c['class'] in main['classes']]
    focal = [m for m in main['methods'] if m['eligible'] and m['class'] in {c['class'] for c in scope}]
    write_json(exp/'population/scope.json', dict(classes=scope, m=len(focal)))
    write_csv(exp/'population/methods.csv', [{k:m[k] for k in ('class','method','signature','line')} for m in focal], ['class','method','signature','line'])
    if g is not None:
        # The baseline round rules consult this file for the matching target.
        write_csv(exp/'dev/dev-denominator.csv', [dict(id=i) for i in range(dev_target)], ['id'])
        if focal:
            generate(exp, config, group=group)
        kept = []
        for directory in rounds(exp/'generated'/group):
            kept, _ = screen_round(exp, config, g, directory, kept)
        method = g['method']
        write_json(exp/'funnel.json', [read_json(d/'funnel.json') for d in rounds(exp/'generated'/group)])
    else:
        method = 'dev'
        members = []
        for row in tests['methods']:
            if not row['kind']:
                continue
            t = frozen_method({k:row[k] for k in ('class','method','kind')})
            # Nested classes live in their enclosing source file.
            t.update(body_hash=row['body_hash'], source_root='dev/src-test-t/java', resource_root='dev/src-test-t',
                     file_hash=hashlib.sha256((exp/t['file']).read_bytes()).hexdigest())
            members.append(t)
        full = coverage(adapter, 0, members, 'dev-full', full_suite=True)
        scope_files = {c['file'] for c in scope}
        observed = {(m['class'],m['method']):m for m in full['methods']}
        if b.dev[group]['denominator'] == 'coverage-scoped':
            selected = [t for t in members if any(f in scope_files for f, _ in observed[(t['class'],t['method'])]['lines'])]
        else:
            chosen = denominator([dict(t, stable='yes') for t in members], b.dev[group]['denominator'], scope,
                                 {c.rsplit('.',1)[-1] for c in main['classes']})
            ids = {(r['class'],r['method']) for r in chosen}
            selected = [t for t in members if (t['class'],t['method']) in ids]
        # Strict 3/3: no flaky rescue in pool screening.
        record = adapter.worker(dict(action='run', index=0, sha=point['sha'], group=group, phase='policy-screen', methods=selected,
                                     repeats=3, retry=False, environment_retry=False, input_sha256=canonical_hash(selected),
                                     runner_sha256=adapter.provenance(), output='results/dev-screen.json', work='work/dev-screen'))
        seen = set(); kept = []
        for t in record['methods']:
            if t['state'] == 'pass' and t['body_hash'] not in seen:
                kept.append(t); seen.add(t['body_hash'])
        write_json(exp/'per-test-coverage.json', full)
        write_json(exp/'funnel.json', dict(discovered=len(members), selected=len(selected), stable_3_of_3=sum(t['state']=='pass' for t in record['methods']), kept=len(kept)))
    # Sources already live beneath the new experiment; normalize all references to its root.
    prefix = str(exp.relative_to(b.exp))
    pool = snapshot(exp, b.exp, kept, group=group, method=method, scope=b.scope['classes'], prefix=prefix)
    write_json(exp/'pool.json', pool)
    return pool


def matching_target(b, plan, group, index, pools):
    devs = [name for name in b.groups if name in b.dev and b.dev[name]['denominator'] in ('scope-targeted','coverage-scoped')]
    dev = devs[0] if devs else next((name for name in b.dev if b.dev[name]['denominator'] in ('scope-targeted','coverage-scoped')), next(iter(b.dev)))
    if (dev,index) in pools:
        return len(pools[dev,index])
    targets = []
    for policy in b.options['set']:
        relevant = [p for p in plan['points'] if p['trigger'] == policy['trigger'] and p['index'] is not None]
        if not any(p['group'] == group and p['index'] == index for p in relevant):
            continue
        preceding = sorted({p['index'] for p in relevant if p['group'] == dev and p['index'] < index})
        if not preceding:
            targets.append(len(b.frozen[dev])); continue
        point = preceding[-1]
        if policy['action'] == 'reset':
            targets.append(len(pools[dev,point])); continue
        # Dev fill includes baseline survivors plus every other pool ID. With a
        # single threshold trigger this can be resolved entirely from baseline evidence.
        if len(preceding) > 1:
            raise ValueError('until_dev_matched needs a same-index Dev pool after multiple Dev fill points')
        survivors = {t['id']:t for t in b.frozen[dev]}
        for commit in b.curves:
            i = int(commit['index'])
            if i > point: break
            if i and b.evaluable(i):
                for t in b.raw(i,dev)['methods']:
                    if t['state'] != 'pass': survivors.pop(t['id'],None)
        identities = {t['class']+'#'+t['method'] for t in survivors.values()}
        identities.update(t['class']+'#'+t['method'] for t in pools[dev,point])
        targets.append(len(identities))
    # A pool shared by policies must be large enough for every requesting target.
    return max(targets, default=len(b.frozen[dev]))


def run(b, plan, step):
    semaphores = {g:threading.Semaphore(b.options['concurrency'].get('dev_selection' if g in b.dev else g, 1)) for g in b.groups}
    dev_lock = threading.Semaphore(b.options['concurrency'].get('dev_selection', 1))
    pools = {}
    def build(point):
        g, i = point['group'], point['index']
        generator = next((item for item in b.execution_config['generators'] if item['name'] == g), None)
        target = matching_target(b,plan,g,i,pools) if generator and generator['rounds']['until_dev_matched'] else len(next(iter(b.frozen.values())))
        path = b.exp/f'results/policy/pools/{g}/{i}/pool.json'
        with dev_lock if g in b.dev else semaphores[g]:
            step(f'pool-{g}-{i}', lambda: build_pool(b, point, target), outputs=[path])
        return (g,i), read_json(path)
    # Developer targets are dependencies of generators; each wave is otherwise independent.
    for developer in (True, False):
        selected = [p for p in plan['pools'] if (p['group'] in b.dev) == developer]
        with ThreadPoolExecutor(max_workers=max(1, sum(b.options['concurrency'].values()))) as executor:
            pools.update(executor.map(build, selected))
    return pools
