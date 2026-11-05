from contextlib import nullcontext
from copy import deepcopy
import hashlib
import subprocess

import pytest

from test_survival.config import validate, stage_hash, STAGES
from test_survival.io import read_json, read_csv, write_json, write_csv
from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.stages import batch, control, survive, cover
from test_survival.progress import items


def fixture(exp):
    config=validate(dict(name='batch',anchor='t',project={'repo':str(exp)},
                        survival={'execution':'batch'},range={'skip_untouched':'main-sources'},
                        dev={'groups':[{'name':'dev'}, {'name':'child','derive_from':'dev'}]},
                        generators=[{'name':'o','method':'ouedraogo','impl':'native'}]))
    groups={}
    for group,path,names in [('dev','dev/src-test-t/java/T.java','ab'),('o','generated/o/round1/sources/A/ZSL/G.java','c')]:
        source=exp/path;source.parent.mkdir(parents=True,exist_ok=True);source.write_text('class '+source.stem+' {}')
        groups[group]=[dict(id=path+'|'+source.stem+'|'+n,file=path,**{'class':source.stem},method=n,kind='plain') for n in names]
    groups['child']=[groups['dev'][1]]
    write_json(exp/'results/frozen-denominators.json',groups)
    write_json(exp/'results/denominators.json',{g:len(ts) for g,ts in groups.items()})
    write_json(exp/'results/anchor.json',dict(sha='t',date='date'))
    write_json(exp/'population/scope.json',dict(classes=[dict(file='src/main/java/A.java',**{'class':'A'})]))
    commits=[dict(index=i,sha=str(i),date='date',subject='change',**{'changes-src-main':'no' if i in (1,3) else 'yes','changes-pom':'no'}) for i in range(1,7)]
    write_csv(exp/'results/commits.csv',commits,list(commits[0]))
    write_json(exp/'results/commit-paths.json',{str(i):['README.md' if i in (1,3) else 'src/main/java/A.java'] for i in range(1,7)})
    for i in range(7):write_json(exp/f'results/builds/{i}.json',dict(sha=str(i) if i else 't',build='fail' if i==5 else 'skipped-untouched' if i in (1,3) else 'ok'))
    return config


def setup_worker(monkeypatch, jobs, missing=False):
    monkeypatch.setattr(batch,'measurement_session',lambda *args:nullcontext())
    monkeypatch.setattr(cover,'measurement_session',lambda *args:nullcontext())
    monkeypatch.setattr(JavaMavenAdapter,'git',lambda *args:'--- a/src/main/java/A.java\n+++ b/src/main/java/A.java\n@@ -1,2 +1,2 @@\n-old\n+new')
    def worker(self,job):
        jobs.append(deepcopy(job))
        return dict(index=job['index'],sha=job['sha'],status='evaluated',coverage_version=3,
                    coverage_status='missing' if missing else 'complete',coverage_reason='timeout' if missing else '',
                    universe=[['src/main/java/A.java',1],['src/main/java/A.java',2]],files=[],compile=[],runs=[],
                    methods=[dict(t,state='fail' if t['method']=='a' and job['index'] else 'pass',message='',
                                  **({} if missing and job['index'] else {'lines':[['src/main/java/A.java',2 if t['method']=='b' else 1]]})) for t in job['methods']])
    monkeypatch.setattr(JavaMavenAdapter,'worker',worker)


@pytest.mark.parametrize('workers',[1,3])
def test_batch_scope_projection_carries_and_resume(tmp_path,monkeypatch,workers):
    config=fixture(tmp_path);config['runner']['workers']['survive']=workers
    jobs=[];setup_worker(monkeypatch,jobs)
    control.run(tmp_path,config);survive.run(tmp_path,config);cover.run(tmp_path,config)
    assert [j['index'] for j in jobs].count(0)==1 and {j['index'] for j in jobs}=={0,2,4,6}
    assert all(len([t for t in j['methods'] if t['method']=='b'])==1 for j in jobs)
    if workers==1:
        assert all(t['method']!='a' for j in jobs if j['index']>2 for t in j['methods'])
    assert read_json(tmp_path/'results/coverage-index-aliases.json')=={'1':0,'3':2}
    rows=read_csv(tmp_path/'results/survival.csv')
    assert [r['dev_alive'] for r in rows]==['2','2','1','1','1','1','1']
    assert [r['evaluated'] for r in rows]==['yes','no','yes','no','yes','no','yes']
    assert len(read_csv(tmp_path/'results/first-failures.csv'))==1
    coverage={int(r['index']):r for r in read_csv(tmp_path/'results/changed-lines.csv')}
    assert coverage[2]['scope_dev_covered']=='2'
    assert coverage[4]['scope_dev_covered']=='1' and coverage[4]['scope_child_covered']=='1'
    assert coverage[5]['scope_dev_covered']==''
    assert all(r['all_executable']==r['other_dev_covered']=='' for r in coverage.values())
    assert not list((tmp_path/'results/coverage').glob('*.xml*'))
    assert not list((tmp_path/'results/raw').glob('1-dev.json'))  # compressed skip marker
    assert read_json(tmp_path/'results/raw/1-dev.json')['status']=='skipped-untouched'
    before=len(jobs)
    control.run(tmp_path,config);survive.run(tmp_path,config);cover.run(tmp_path,config)
    assert len(jobs)==before
    complete,total,_=items(tmp_path,'survive',config,{})
    assert len(complete)==total==6
    complete,total,_=items(tmp_path,'cover',config,{})
    assert len(complete)==total
    assert batch.stage_complete(tmp_path,config,'survive')
    from test_survival.io import resolve_path
    resolve_path(tmp_path/'results/raw/2-dev.json').unlink()
    assert not batch.stage_complete(tmp_path,config,'survive')
    survive.run(tmp_path,config)
    assert len(jobs)==before


def test_missing_lines_remain_unavailable_and_remeasure_keeps_verdict(tmp_path,monkeypatch):
    config=fixture(tmp_path);config['runner']['workers']['survive']=1
    jobs=[];setup_worker(monkeypatch,jobs,missing=True)
    control.run(tmp_path,config);survive.run(tmp_path,config);cover.run(tmp_path,config)
    before=(tmp_path/'results/survival.csv').read_bytes()
    assert read_json(tmp_path/'results/raw-coverage/2-dev.json')['coverage_status']=='missing'
    assert read_json(tmp_path/'results/coverage-consistency.json')['missing']
    setup_worker(monkeypatch,jobs)
    survive.run(tmp_path,config);cover.run(tmp_path,config)
    assert (tmp_path/'results/survival.csv').read_bytes()==before
    assert read_json(tmp_path/'results/raw-coverage/2-dev.json')['coverage_status']=='complete'


def test_mode_hash_boundaries_and_historical_raw_fingerprint(tmp_path):
    raw=dict(name='test',anchor='t',project={'repo':str(tmp_path)})
    legacy=validate(raw);changed=validate({**raw,'survival':{'execution':'batch'}})
    assert [stage_hash(legacy,s)!=stage_hash(changed,s) for s in STAGES]==[False]*4+[True]*4
    a=JavaMavenAdapter(tmp_path,legacy)
    original=deepcopy(legacy);original['survival'].pop('execution')
    assert a.run_fingerprints([])==JavaMavenAdapter(tmp_path,original).run_fingerprints([])
    assert changed['survival']['flaky_rerun']==1  # accepted, ignored by batch measurement


def test_snapshot_helper_change_invalidates_batch(tmp_path,monkeypatch):
    config=fixture(tmp_path);jobs=[];setup_worker(monkeypatch,jobs)
    control.run(tmp_path,config)
    (tmp_path/'dev/src-test-t/java/Helper.java').write_text('class Helper {}')
    control.run(tmp_path,config)
    assert len(jobs)==2


from test_junit4 import java_environment
from test_policy import baseline
from test_survival.policy.baseline import Baseline
from test_survival.policy.execute import Runs, baseline_tests
from test_survival.policy.pipeline import Steps
from test_survival.policy.plan import build_plan


def test_policy_imports_batch_lines_without_execution(baseline,monkeypatch):
    exp,config,_=baseline;b=Baseline(exp,config)
    frozen=baseline_tests(b);steps=Steps(b,build_plan(b));runs=Runs(b,steps)
    for group,tests in frozen.items():
        runs.register(tests)
        raw=b.raw(1,group)
        by_id={t['origin_id']:t for t in tests}
        raw.update(execution='batch',coverage_version=3,sha=b.by_index[1]['sha'])
        for t in raw['methods']:
            t.update(execution_hash=by_id[t['id']]['execution_hash'],lines=[['src/main/java/A.java',1]])
        write_json(b.raw_path(1,group),raw)
    monkeypatch.setattr(runs.adapter,'worker',lambda *_:pytest.fail('batch baseline was reexecuted'))
    runs.run_index(1,sorted(runs.catalog))
    assert all(read_json(runs.path(1,h))['measurement_source']=='batch baseline' for h in runs.catalog)


@pytest.mark.java_integration
def test_scope_universe_without_tests_or_probe_data(tmp_path,java_environment):
    image,libs=java_environment
    root=__import__('pathlib').Path(__file__).parents[1]
    (tmp_path/'A.java').write_text('package fixture; public class A { public int a() { return 1; } }')
    (tmp_path/'Other.java').write_text('package fixture; public class Other { public int b() { return 2; } }')
    (tmp_path/'scope.txt').write_text('src/main/java/fixture/A.java\n')
    script='''from pathlib import Path
import subprocess
cp=':'.join(str(p) for p in Path('/libs').glob('*.jar') if 'jacoco' not in p.name or '0.8.14' in p.name)
subprocess.run(['javac','-d','/out/classes','/out/A.java','/out/Other.java'],check=True)
subprocess.run(['javac','-cp',cp,'-d','/out/helper','/helpers/PerTestCoverage.java'],check=True)
subprocess.run(['java','-cp','/out/helper:'+cp,'PerTestCoverage','--universe','/out/classes','/out/lines.tsv','/out/scope.txt'],check=True)
'''
    subprocess.run(['docker','run','--rm','--network','none','-v',str(root/'src/test_survival/adapters/java')+':/helpers:ro',
                    '-v',str(libs)+':/libs:ro','-v',str(tmp_path)+':/out',image,'python3','-c',script],check=True,timeout=120,capture_output=True)
    assert (tmp_path/'lines.tsv').read_text()=='src/main/java/fixture/A.java\t1\n'
    assert not list(tmp_path.rglob('*.exec')) and not list(tmp_path.rglob('*.xml'))


@pytest.mark.parametrize('with_generator',[False, True])
def test_local_children_inherit_imported_batch_parent(tmp_path, monkeypatch, with_generator):
    import yaml
    from test_survival.dev_results import import_results, file_hash
    from test_survival.rules.dev_denominator import denominator_file
    source, target = tmp_path/'source', tmp_path/'target'
    configs = {}
    jobs = []; setup_worker(monkeypatch, jobs)
    for exp in (source, target):
        c = fixture(exp); c['project']['repo'] = str(tmp_path/'repo')
        if exp == source:
            c['dev']['groups'] = c['dev']['groups'][:1]
            frozen = read_json(exp/'results/frozen-denominators.json'); frozen.pop('child')
        else:
            leaf = dict(c['dev']['groups'][1], name='leaf', derive_from='child')
            c['dev']['groups'].append(leaf)
            frozen = read_json(exp/'results/frozen-denominators.json')
            frozen = {'leaf':frozen['child'], 'child':frozen['child'], 'o':frozen['o'], 'dev':frozen['dev']}
        if not with_generator:
            c['generators'] = []; frozen.pop('o')
        write_json(exp/'results/frozen-denominators.json', frozen)
        for i,g in enumerate(c['dev']['groups']):
            write_csv(exp/denominator_file(g,i), frozen[g['name']], list(frozen[g['name']][0]))
        (exp/'experiment.yaml').write_text(yaml.safe_dump(dict(name='fixture', anchor='t',
            project=c['project'], survival={'execution':'batch'}, range={'skip_untouched':'main-sources'}, dev={'groups':c['dev']['groups']})))
        configs[exp] = c
    control.run(source,configs[source]); survive.run(source,configs[source]); cover.run(source,configs[source])
    source_bytes = {p:p.read_bytes() for p in source.rglob('*') if p.is_file()}
    provenance = import_results(target,source,configs[target])
    write_json(target/'state/import.json', {'dev-results':provenance})
    sessions=[]
    monkeypatch.setattr(batch,'measurement_session',lambda adapter,phase,enabled:sessions.append(enabled) or nullcontext())
    control.run(target,configs[target])
    jobs.clear()
    original = JavaMavenAdapter.worker
    def disagree(self,job):
        raw = original(self,job)
        for t in raw['methods']:
            if t['method']=='b':t.update(state='fail',lines=[])
        return raw
    monkeypatch.setattr(JavaMavenAdapter,'worker',disagree)
    survive.run(target,configs[target]); cover.run(target,configs[target])
    assert bool(jobs)==with_generator
    assert sessions==[with_generator,with_generator]
    assert all(t['method']=='c' for j in jobs for t in j['methods'])
    for index in (2,4,6):
        parent = read_json(target/f'results/raw/{index}-dev.json')
        for g in ('child','leaf'):
            child = read_json(target/f'results/raw-derived/{index}-{g}.json')
            assert child['methods']==[t for t in parent['methods'] if t['method']=='b']
            assert child['derived_from']==('dev' if g=='child' else 'child')
            assert any(r['covered']=='yes' for r in read_csv(target/f'results/coverage/{index}-{g}-lines.csv'))
    assert all(file_hash(target/p)==h for p,h in provenance['files'].items())
    assert all(p.read_bytes()==data for p,data in source_bytes.items())
    before = len(jobs)
    assert batch.stage_complete(target,configs[target],'survive')
    survive.run(target,configs[target])
    assert len(jobs)==before


@pytest.mark.parametrize('repair_state',['compile-fail','absent'])
def test_nonexecuting_coverage_repair_is_retried(tmp_path,monkeypatch,repair_state):
    config=fixture(tmp_path);config['runner']['workers']['survive']=1
    jobs=[];setup_worker(monkeypatch,jobs,missing=True)
    control.run(tmp_path,config);survive.run(tmp_path,config);cover.run(tmp_path,config)
    before=(tmp_path/'results/survival.csv').read_bytes()
    setup_worker(monkeypatch,jobs)
    original=JavaMavenAdapter.worker
    def unavailable(self,job):
        raw=original(self,job)
        for t in raw['methods']:
            if job['index']==2 and t['method']=='a':t.update(state=repair_state,lines=[])
        return raw
    monkeypatch.setattr(JavaMavenAdapter,'worker',unavailable)
    survive.run(tmp_path,config);cover.run(tmp_path,config)
    assert read_json(tmp_path/'results/raw-coverage/2-dev.json')['coverage_status']=='missing'
    assert not batch.stage_complete(tmp_path,config,'survive')
    setup_worker(monkeypatch,jobs);start=len(jobs)
    survive.run(tmp_path,config);cover.run(tmp_path,config)
    assert [(j['index'],[t['method'] for t in j['methods']]) for j in jobs[start:]]==[(2,['a'])]
    assert (tmp_path/'results/survival.csv').read_bytes()==before
    assert read_json(tmp_path/'results/raw-coverage/2-dev.json')['coverage_status']=='complete'
    assert batch.stage_complete(tmp_path,config,'survive')


def test_empty_local_populations_still_measure_scope_universe(tmp_path,monkeypatch):
    config=fixture(tmp_path)
    write_json(tmp_path/'results/frozen-denominators.json', {g:[] for g in ('dev','child','o')})
    jobs=[];setup_worker(monkeypatch,jobs)
    control.run(tmp_path,config);survive.run(tmp_path,config);cover.run(tmp_path,config)
    assert {j['index'] for j in jobs}=={0,2,4,6}
    assert all(j['methods']==[] for j in jobs)
    lines=read_csv(tmp_path/'results/coverage/2-child-lines.csv')
    assert len(lines)==2 and all(r['covered']=='no' for r in lines)
    assert batch.stage_complete(tmp_path,config,'survive')


@pytest.mark.parametrize('damage', ['missing-lines', 'nonexecuting-repair', 'projection', 'identity'])
def test_batch_progress_matches_resume_and_is_read_only(tmp_path, monkeypatch, damage):
    from test_survival.progress import panel_items, describe
    from test_survival.io import resolve_path
    config = fixture(tmp_path); config['runner']['workers']['survive'] = 1
    jobs = []; setup_worker(monkeypatch, jobs)
    control.run(tmp_path, config); survive.run(tmp_path, config)
    path = tmp_path/'results/baseline-batches/2.json'
    raw = read_json(path)
    if damage == 'projection':
        resolve_path(tmp_path/'results/raw-derived/2-child.json').unlink()
    else:
        if damage == 'missing-lines':
            raw['methods'][0].pop('lines')
        elif damage == 'nonexecuting-repair':
            raw['methods'][0].update(lines=[], remeasured_state='compile-fail')
        else:
            raw['input_sha256'] = 'outdated'
        write_json(path, raw, compress=True)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    before_jobs = len(jobs)
    assert not batch.stage_complete(tmp_path, config, 'survive')
    completed, total, unit = items(tmp_path, 'survive', config, {})
    assert total == 6 and unit == 'commits' and len(completed) < total
    if damage != 'identity':
        assert len(completed) == 5  # Death verdict still proves later dead omissions.
    assert panel_items(tmp_path, 'survive', config, {})[:3] == (completed, total, unit)
    assert f'{len(completed)}/6 commits' in describe(tmp_path, 'survive', config)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert len(jobs) == before_jobs
    survive.run(tmp_path, config)
    assert batch.stage_complete(tmp_path, config, 'survive')
    assert len(items(tmp_path, 'survive', config, {})[0]) == 6
    if damage == 'projection':
        assert len(jobs) == before_jobs
    elif damage != 'identity':
        assert [(j['index'], [t['method'] for t in j['methods']]) for j in jobs[before_jobs:]] == [(2, ['a'])]


def test_batch_control_progress_requires_reusable_measurement(tmp_path, monkeypatch):
    from test_survival.progress import panel_items
    config = fixture(tmp_path)
    jobs = []; setup_worker(monkeypatch, jobs)
    control.run(tmp_path, config)
    path = tmp_path/'results/baseline-batches/0.json'
    raw = read_json(path); raw['methods'][0].pop('lines')
    write_json(path, raw, compress=True)
    assert not batch.stage_complete(tmp_path, config, 'control')
    assert items(tmp_path, 'control', config, {'status':'completed'})[:2] == ([], 1)
    assert panel_items(tmp_path, 'control', config, {})[:3] == ([], 4, 'methods')
    control.run(tmp_path, config)
    assert batch.stage_complete(tmp_path, config, 'control')
    completed, total, _, _ = panel_items(tmp_path, 'control', config, {})
    assert len(completed) == total == 4
