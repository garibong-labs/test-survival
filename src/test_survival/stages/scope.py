import collections
import csv
import io
import json
import hashlib
import time

from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json, read_csv, write_json, write_csv, atomic_text
from ..rules.scope import select_scope
from ..rules.dev_denominator import denominator,coverage_scoped,validate_derived_subsets,frozen_method,denominator_file,identity
from ..rules.matching import renumber


def candidate_coverage(exp, config, adapter, baseline):
    """Reuse full-candidate coverage when only the proposed scope changes."""
    from ..config import canonical_hash, record_runner_inputs
    from ..policy.artifacts import tree_hash
    candidates = []
    for row in baseline:
        # Source discovery historically measured stable methods only. Its
        # audited overloads remain duplicate unstable rows, not executable IDs.
        if config['dev'].get('discovery', 'source') == 'source' and row['stable'] != 'yes':
            continue
        t = frozen_method(identity(row))
        t.update(source_root='dev/src-test-t/java', resource_root='dev/src-test-t',
                 file_hash=hashlib.sha256((exp/t['file']).read_bytes()).hexdigest())
        candidates.append(t)
    anchor = read_json(exp/'results/anchor.json')['sha']
    fingerprint = canonical_hash(dict(sha=anchor, methods=candidates, tree=tree_hash(exp/'dev/src-test-t'),
        project=config['project'], runner=record_runner_inputs(config['runner']),
        selection=config['survival'].get('selection', 'all'), provenance=adapter.provenance()))
    output = 'results/dev-candidate-coverage-t.json'
    record = read_json(exp/output, {})
    reused = (record.get('input_sha256') == fingerprint and record.get('coverage_status') == 'complete'
              and {(m['class'],m['method']) for m in record.get('methods', [])} ==
                  {(m['class'],m['method']) for m in candidates})
    if not reused:
        adapter.setup_runner()
        start = time.monotonic()
        with adapter.runner.session('method-coverage'):
            record = adapter.worker(dict(action='policy-cover', index=0, sha=anchor,
                group=config['dev']['groups'][0]['name'], methods=candidates, candidate_suite=True,
                output=output, work='work/dev-candidate-coverage-t'))
        record.update(input_sha256=fingerprint, measurement_seconds=time.monotonic()-start)
        write_json(exp/output, record)
    write_json(exp/'results/dev-candidate-coverage-t-timing.json', dict(seconds=record['measurement_seconds'],
        methods=len(candidates), test_jvm_seconds=sum(r['seconds'] for r in record['runs']),
        jvms=len(record['runs']), reused=reused))
    return record


def run(exp, config):
    from ..dev_results import imported_groups, check_imported_scope, load_provenance
    imported = imported_groups(exp, config)
    adapter = JavaMavenAdapter(exp, config)
    adapter.ensure_anchor()
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
    groups=config['dev']['groups'];denominators={};excluded_counts={};observations=[]
    scoped_groups = [g for g in groups if g['denominator'] in ('coverage-scoped', 'scope-targeted') and g['name'] not in imported]
    bounded = config['scope']['rule'] in ('change-ranked', 'dedicated-test') and bool(scoped_groups)
    needs_coverage = any(g['denominator']=='coverage-scoped' and g['name'] not in imported for g in groups)
    measured = []
    seconds = 0
    if needs_coverage:
        record = candidate_coverage(exp, config, adapter, baseline)
        seconds = record['measurement_seconds']
        measured = record['methods']
    stable = {(r['class'],r['method']) for r in baseline if r['stable']=='yes'}
    production_names = {n.rsplit('.',1)[-1] for n in inventory['main']['classes']}
    def dev_size(rows):
        files = {r['file'] for r in rows}
        sizes = []
        for group in scoped_groups:
            if group['denominator'] == 'coverage-scoped':
                sizes.append(sum((m['class'],m['method']) in stable and m['state']=='pass' and
                                 any(f in files for f, _ in m.get('lines', [])) for m in measured))
            else:
                sizes.append(len(denominator(baseline, 'scope-targeted', rows, production_names)))
        return max(sizes, default=0)
    skipped = []
    retained = load_provenance(exp).get('scope_selection', {}) if imported else {}
    if (retained.get('config') == config['scope'] and
            isinstance(retained.get('population', {}).get('classes'), list) and
            all(isinstance(row, dict) and any(row.get('class') == r['class'] and row.get('file') == r['file'] for r in survey)
                for row in retained['population']['classes'])):
        by_name = {row['class']: row for row in survey}
        chosen = [by_name[row['class']] for row in retained['population']['classes']]
        _, eligible = select_scope(survey, config['scope'])
        skipped = retained.get('selection', {}).get('skipped_classes', [])
    else:
        chosen, eligible = (select_scope(survey, config['scope'], dev_size, skipped) if bounded
                            else select_scope(survey, config['scope']))
    if imported:
        check_imported_scope(exp, config, chosen)
    selected = set(r['class'] for r in chosen)
    focal = [m for m in inventory['main']['methods'] if m['eligible'] and m['class'] in selected]
    scope_files = {r['file'] for r in chosen}
    for m in measured:
        covered = m.get('lines', [])
        scoped = [(f,n) for f,n in covered if f in scope_files]
        observations.append({'class':m['class'], 'method':m['method'], 'scope_lines_covered':len(scoped),
            'scope_files':json.dumps(sorted({f for f,_ in scoped})), 'total_lines_covered':len(covered),
            'seconds':'', 'status':m['state']})
    if needs_coverage:
        write_csv(exp/'results/dev-method-coverage-t.csv', observations,
                  ['class','method','scope_lines_covered','scope_files','total_lines_covered','seconds','status'])
    for index,g in enumerate(groups):
        if g['name'] in imported:
            selected_dev = [identity(row) for row in imported[g['name']]['frozen']]
            excluded_path = exp/f"dev/{g['name']}-excluded.csv"
            excluded = read_csv(excluded_path) if excluded_path.exists() else []
        elif g['denominator']=='coverage-scoped':
            selected_dev,excluded=coverage_scoped(baseline,observations)
        else:
            selected_dev=denominator(baseline,g['denominator'],chosen,{n.rsplit('.',1)[-1] for n in inventory['main']['classes']})
            selected_methods={(r['class'],r['method']) for r in selected_dev}
            excluded=[{**identity(r),'status':'pass','scope_lines_covered':'','reason':'not-scope-targeted'} for r in baseline if r['stable']=='yes' and (r['class'],r['method']) not in selected_methods]
        denominators[g['name']]=selected_dev
        fields = ['class','method','kind']
        if any(r.get('file') for r in selected_dev): fields += ['file']
        if any(r.get('declaring_file') for r in selected_dev): fields += ['declaring_class','declaring_file']
        write_csv(exp/denominator_file(g,index),selected_dev,fields)
        excluded_fields = ['class','method','kind','status','scope_lines_covered','reason']
        if any(r.get('file') for r in excluded): excluded_fields += ['file']
        if any(r.get('declaring_file') for r in excluded): excluded_fields += ['declaring_class','declaring_file']
        write_csv(exp/f"dev/{g['name']}-excluded.csv",excluded,excluded_fields)
        excluded_counts[g['name']]=dict(excluded=len(excluded),failed_alone=sum(r['reason']=='failed-alone' for r in excluded))
    validate_derived_subsets(groups,denominators)
    write_json(exp/'results/dev-group-selection.json',dict(denominators={g:len(rows) for g,rows in denominators.items()},exclusions=excluded_counts))
    dev=denominators[groups[0]['name']]
    table(exp/'population/methods.csv', focal, ['class','method','signature','line'])
    write_json(exp/'population/scope.json', dict(classes=chosen, m=len(focal), d=sum(r['d'] for r in chosen)))
    stop = len(chosen) < config['scope']['min_classes']
    selection = dict(classes=len(chosen), m=len(focal), developer_denominator=len(dev), eligible_classes=eligible,
                     max_dev_scope=config['scope'].get('max_dev_scope',400), skipped_classes=skipped, per_test_coverage_seconds=seconds,
                     mandatory_stop=stop, stop_reason='Fewer classes than scope.min_classes' if stop else None, model_calls=0)
    write_json(exp/'results/scope-selection.json', selection)
    atomic_text(exp/'population/scope.md', '# Selected scope\n\n'+f'Rule: {config["scope"]["rule"]}.\n\n'+'\n'.join(f'- {r["class"]}: m={r["m"]}, c={r["c"]}, accessors={r["accessors"]}.' for r in chosen)+'\n\n## Classes skipped by developer scope bound\n\n'+'\n'.join(f"- {r['class']}: c={r['c']}, would-be Dev(scope)={r['would_be_dev_scope']}." for r in skipped)+'\n')
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
