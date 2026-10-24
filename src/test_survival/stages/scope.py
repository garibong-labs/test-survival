import collections
import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor

from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json, read_csv, write_json, write_csv, atomic_text
from ..rules.scope import select_scope
from ..rules.dev_denominator import denominator,coverage_scoped,validate_derived_subsets,frozen_method,denominator_file
from ..rules.matching import renumber


def run(exp, config):
    adapter = JavaMavenAdapter(exp, config)
    inventory = read_json(exp/'results/source-inventory.json')
    baseline = read_csv(exp/'dev/dev-methods-all.csv')
    touches = read_json(exp/'results/commit-paths.json')
    methods = collections.defaultdict(list)
    for method in inventory['main']['methods']:
        if method['eligible']:
            methods[method['class']].append(method)
    audit_log = exp/'population/accessor-methods.tsv'
    result = adapter.runner.execute(['java', '-Xmx'+config['runner']['jvm']['launcher'], str(adapter.helpers/'AccessorAudit.java'), str(adapter.folder(0)/'src/main/java')], adapter.folder(0), audit_log, config['runner']['timeouts']['compile'])
    if result['exit_code']:
        raise RuntimeError('Accessor inventory failed')
    text = audit_log.read_text()
    text = text[text.index('class\tmethod\t'):]
    audits = list(csv.DictReader(io.StringIO(text), delimiter='\t'))
    counts = collections.Counter(r['class'] for r in audits if r['accessor']=='true')
    module = config['project']['module'].removeprefix('./').strip('/')
    prefix = '' if module in ['', '.'] else module+'/'
    survey = []
    for cls, members in methods.items():
        file = inventory['main']['classes'][cls]['path']
        test = cls+'Test'
        has_test = test in inventory['test']['classes'] and inventory['test']['classes'][test]['path']=='src/test/java/'+test.replace('.','/')+'.java'
        survey.append(dict(**{'class':cls}, file=file, m=len(members), has_test='yes' if has_test else 'no',
                           d=sum(r['class']==test and r['stable']=='yes' for r in baseline), c=sum(prefix+file in paths for paths in touches.values()),
                           accessors=counts[cls], accessor_fraction=counts[cls]/len(members), excluded_accessors='yes' if counts[cls]*2>len(members) else 'no'))
    survey.sort(key=lambda r:(-r['c'],-r['d'],r['class']))
    def table(path, rows, fields):
        write_csv(path, [{k:r[k] for k in fields} for r in rows], fields)
    table(exp/'population/class-survey.csv', survey, ['class','file','m','has_test','d','c'])
    table(exp/'population/accessor-audit.csv', survey, ['class','m','accessors','accessor_fraction','excluded_accessors'])
    chosen, eligible = select_scope(survey, config['scope'])
    selected = set(r['class'] for r in chosen)
    focal = [m for m in inventory['main']['methods'] if m['eligible'] and m['class'] in selected]
    groups=config['dev']['groups'];denominators={};excluded_counts={};observations=[]
    if any(g['denominator']=='coverage-scoped' for g in groups):
        adapter.setup_runner()
        stable=[r for r in baseline if r['stable']=='yes'];scope_files={r['file'] for r in chosen}
        def coverage(row):
            raw=adapter.method_coverage(frozen_method({k:row[k] for k in ['class','method','kind']}))
            lines=read_csv(exp/f"results/dev-method-coverage-t/{row['class']}#{row['method']}-lines.csv")
            covered=[r for r in lines if r['covered']=='yes'];scoped=[r for r in covered if r['file'] in scope_files]
            return {'class':row['class'],'method':row['method'],'scope_lines_covered':len(scoped),
                    'scope_files':json.dumps(sorted({r['file'] for r in scoped})), 'total_lines_covered':len(covered),
                    'seconds':raw['seconds'],'status':raw['methods'][0]['state'] if raw['status']=='evaluated' else raw['status']}
        with adapter.runner.session('method-coverage'),ThreadPoolExecutor(max_workers=4) as pool:
            observations=list(pool.map(coverage,stable))
        write_csv(exp/'results/dev-method-coverage-t.csv',observations,['class','method','scope_lines_covered','scope_files','total_lines_covered','seconds','status'])
    for index,g in enumerate(groups):
        if g['denominator']=='coverage-scoped':
            selected_dev,excluded=coverage_scoped(baseline,observations)
        else:
            selected_dev=denominator(baseline,g['denominator'],chosen,{n.rsplit('.',1)[-1] for n in inventory['main']['classes']})
            selected_methods={(r['class'],r['method']) for r in selected_dev}
            excluded=[{**{k:r[k] for k in ['class','method','kind']},'status':'pass','scope_lines_covered':'','reason':'not-scope-targeted'} for r in baseline if r['stable']=='yes' and (r['class'],r['method']) not in selected_methods]
        denominators[g['name']]=selected_dev
        write_csv(exp/denominator_file(g,index),selected_dev,['class','method','kind'])
        write_csv(exp/f"dev/{g['name']}-excluded.csv",excluded,['class','method','kind','status','scope_lines_covered','reason'])
        excluded_counts[g['name']]=dict(excluded=len(excluded),failed_alone=sum(r['reason']=='failed-alone' for r in excluded))
    validate_derived_subsets(groups,denominators)
    write_json(exp/'results/dev-group-selection.json',dict(denominators={g:len(rows) for g,rows in denominators.items()},exclusions=excluded_counts))
    dev=denominators[groups[0]['name']]
    table(exp/'population/methods.csv', focal, ['class','method','signature','line'])
    write_json(exp/'population/scope.json', dict(classes=chosen, m=len(focal), d=sum(r['d'] for r in chosen)))
    stop = len(chosen) < config['scope']['min_classes']
    selection = dict(classes=len(chosen), m=len(focal), developer_denominator=len(dev), eligible_classes=eligible,
                     mandatory_stop=stop, stop_reason='Fewer classes than scope.min_classes' if stop else None, model_calls=0)
    write_json(exp/'results/scope-selection.json', selection)
    atomic_text(exp/'population/scope.md', '# Selected scope\n\n'+f'Rule: {config["scope"]["rule"]}.\n\n'+'\n'.join(f'- {r["class"]}: m={r["m"]}, c={r["c"]}, accessors={r["accessors"]}.' for r in chosen)+'\n')
    if any(g['method']=='ktester' for g in config['generators']):
        entries = []
        for m in focal:
            cls = m['class']; package, _, simple = cls.rpartition('.')
            ident = simple+'_'+m['method']
            test = package+'.'+ident+'_Test'
            entries.append({'id':ident,'class':cls,'package':package,'method-name':m['signature'], 'test-class':test,'test-path':'src/test/java/'+test.replace('.','/')+'.java'})
        table(exp/'population/ktester-population.csv', renumber(entries), ['id','class','method-name','test-class','test-path'])
    if stop:
        raise RuntimeError(selection['stop_reason'])
    return f'{len(chosen)} classes; {len(focal)} focal methods; {eligible} eligible classes; '+', '.join(f'{g}: {len(rows)} stable methods, {excluded_counts[g]["failed_alone"]} failed alone' for g,rows in denominators.items())
