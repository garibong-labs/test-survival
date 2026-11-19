import subprocess

import pytest

from test_batch_baseline import fixture, setup_worker
from test_policy import baseline, test_test
from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import ConfigError, STAGES, as_written, stage_hash, validate
from test_survival.io import read_csv, read_json, write_csv, write_json
from test_survival.policy import cover as policy_cover
from test_survival.policy.affected import logical_path
from test_survival.policy.baseline import Baseline
from test_survival.policy.segments import key, segment
from test_survival.report import consistency
from test_survival.stages import batch, control, cover, survive


A = 'src/main/java/p/Anchor.java'
B = 'src/main/java/other/Added.java'


@pytest.mark.parametrize('coverage', [{}, {'changed_line':False}])
def test_universe_stage_boundaries_and_default_serialization(coverage):
    raw = dict(name='fixture', anchor='t', project={'repo':'.'}, survival={'execution':'batch'})
    old = validate({**raw, **({'coverage':coverage} if coverage else {})})
    scope = validate({**raw, 'coverage':{**coverage,'universe':'scope'}})
    wide = validate({**raw, 'coverage':{**coverage,'universe':'production'}})
    assert as_written(scope) == as_written(old)
    assert dict(scope['coverage']) == dict(old['coverage'])
    assert [stage_hash(scope,s) for s in STAGES] == [stage_hash(old,s) for s in STAGES]
    assert [s for s in STAGES if stage_hash(wide,s) != stage_hash(old,s)] == list(STAGES[4:])
    with pytest.raises(ConfigError, match='coverage.universe'):
        validate({**raw, 'coverage':{'universe':'other'}})


def test_adapter_selects_measured_git_tree_for_batch_and_pool(tmp_path, monkeypatch):
    repo = tmp_path/'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git','-C',str(repo),*args], text=True).strip()
    git('init','-q')
    def commit():
        git('add','.')
        git('-c','user.name=Fixture','-c','user.email=fixture@example.org','commit','-qm','fixture')
        return git('rev-parse','HEAD')
    def add(path):
        file = repo/path
        file.parent.mkdir(parents=True,exist_ok=True)
        file.write_text('class Example {}')
    add('module/'+A)
    add('module/target/generated-sources/Parser.java')
    add('other/'+A)
    anchor = commit()
    add('module/'+B)
    (repo/'module'/A).unlink()
    last = commit()
    cfg = validate(dict(name='x', anchor=anchor, project={'repo':str(repo),'module':'module'},
                        survival={'execution':'batch'}, coverage={'universe':'production'}))
    exp = tmp_path/'out'
    adapter = JavaMavenAdapter(exp,cfg)
    monkeypatch.setattr(adapter, 'ensure_anchor', lambda:None)
    monkeypatch.setattr(adapter, 'build', lambda *_:{'build':'ok'})
    monkeypatch.setattr(adapter.runner, 'execute', lambda *_:{'exit_code':0})
    def manifest(action, **kwargs):
        adapter.worker(dict(action=action, work='work/test', methods=[], **kwargs))
        return read_json(exp/'work/test-job.json')
    assert manifest('policy-survive', index=0, sha=anchor)['scope_files'] == [A]
    assert manifest('policy-survive', index=2, sha=last)['scope_files'] == [B]
    assert manifest('policy-cover', index=0, sha=last, production_pool=True)['scope_files'] == [B]
    assert 'coverage_universe' not in manifest('policy-cover', index=0, sha=anchor, candidate_suite=True)


def test_baseline_partitions_endpoints_and_carried_projection(tmp_path, monkeypatch):
    config = fixture(tmp_path)
    jobs = []
    setup_worker(monkeypatch,jobs)
    control.run(tmp_path,config)
    assert not (tmp_path/'results/coverage-total.csv').exists()
    config['coverage']['universe'] = 'production'
    config['survival']['selection'] = 'affected'
    write_json(tmp_path/'population/scope.json', {'classes':[{'class':'p.Anchor','file':A}]})
    # Index 6 is evaluated but only changes a POM. Index 5 fails to build.
    path = tmp_path/'results/commits.csv'
    commits = read_csv(path)
    commits[-1]['changes-src-main'] = 'no'
    write_csv(path, commits, list(commits[0]))
    paths = read_json(tmp_path/'results/commit-paths.json')
    paths['6'] = ['pom.xml']
    write_json(tmp_path/'results/commit-paths.json', paths)
    diff = f'--- a/{B}\n+++ b/{B}\n@@ -0,0 +1,2 @@\n'
    monkeypatch.setattr(JavaMavenAdapter, 'git', lambda *args:diff)
    monkeypatch.setattr(batch.Selector, 'context', lambda *_:{'known':True,'entries':[]})
    monkeypatch.setattr(batch.Selector, 'decision', lambda self,row,*args: None if row else 'missing')
    def worker(self,job):
        index = job['index']
        jobs.append((index,len(job['methods'])))
        # A carried line disappears from the executable universe at index 2,
        # then returns at 4. Every carry points to index 0.
        universe = ([[A,1]] if index != 2 else []) + ([[B,1],[B,2]] if index else [])
        return dict(index=index, sha=job['sha'], status='evaluated', coverage_version=3, universe=universe, files=[],
                    methods=[dict(t,state='pass',lines=[[A,1]]) for t in job['methods']])
    monkeypatch.setattr(JavaMavenAdapter, 'worker', worker)
    control.run(tmp_path,config)
    survive.run(tmp_path,config)
    cover.run(tmp_path,config)
    assert jobs[-4:] == [(0,3),(2,0),(4,0),(6,0)]
    for i,lines in [(2,[]),(4,[[A,1]]),(6,[[A,1]])]:
        row = read_json(tmp_path/f'results/baseline-batches/{i}.json')['methods'][0]
        assert row['lines'] == lines and row['carried_from_index'] == 0
    assert batch.stage_complete(tmp_path,config,'survive')
    changes = read_csv(tmp_path/'results/changed-lines.csv')
    assert changes[0]['scope_executable'] == '0'
    assert changes[0]['all_executable'] == changes[0]['other_executable'] == '2'
    assert changes[0]['other_dev_covered'] == '0' and changes[0]['all_dev_percent'] == '0.0'
    assert changes[-1]['all_executable'] == ''  # Failed build
    totals = read_csv(tmp_path/'results/coverage-total.csv')
    assert {int(r['index']) for r in totals} == {0,6}
    assert {tuple(r[k] for k in ('files','executable_lines','covered_lines','percent'))
            for r in totals if r['index']=='6'} == {('2','3','1','33.333')}
    assert '33.333' in (tmp_path/'results/coverage-total.md').read_text()
    assert consistency.build(tmp_path,config)['coverage_scope'] == 'production'


@pytest.mark.parametrize('selection', ['all','affected'])
def test_policy_endpoint_uses_regenerated_entrants_and_current_universe(baseline, selection):
    exp, cfg, root = baseline
    b = Baseline(exp,cfg)
    b.execution_config['coverage']['universe'] = 'production'
    b.execution_config['survival']['selection'] = selection
    group = b.groups[0]
    p = 'release-reset'
    b.options['set'] = [b.options['set'][0]]
    member = test_test('new')
    entries = [segment(1,0,[],[]), segment(2,2,[],[member])]
    out = exp/'results/policy'/p
    write_json(out/'segments.json', {group:entries})
    # The entrant fails at the endpoint. Its execution at that index still counts.
    write_json(out/'alive.json', {str(i):{group:[member] if i==2 else []} for i in range(4)})
    rows = [dict(index=i,sha=b.by_index[i]['sha'],group=group,files=1 if not i else 2,
                 executable_lines=1 if not i else 3,covered_lines=0,percent=0.0) for i in (0,3)]
    write_csv(root/'results/coverage-total.csv', rows, list(rows[0]))
    write_csv(root/f'results/coverage/3-{group}-lines.csv',
              [dict(file=f,line=n,covered='no') for f,n in [(A,1),(B,1),(B,2)]], ['file','line','covered'])
    method = dict(member,state='fail',lines=[[B,2]])
    record = logical_path(b,p,group,2,3) if selection=='affected' else exp/f'results/policy/runs/{member["file_hash"]}/3.json'
    write_json(record,dict(methods=[method]))
    change = dict(index=3,sha=rows[-1]['sha'],date='today',subject='new',scope_executable=0,all_executable=2)
    write_csv(root/'results/changed-lines.csv',[change],list(change))
    write_json(root/'results/changes/3.json',dict(post_image={B:[1,2]}))
    policy_cover.run(b,None)
    assert read_csv(out/'coverage-commits.csv')[0][group+'_percent'] == '50.0'
    assert read_csv(out/'coverage-total.csv')[-1]['percent'] == '33.333'
    assert read_csv(exp/'results/policy/none/coverage-total.csv')[-1]['covered_lines'] == '0'
    write_json(record,dict(methods=[]))
    policy_cover.run(b,None)
    assert read_csv(out/'coverage-total.csv')[-1]['covered_lines'] == ''


def test_affected_policy_projects_each_carry_from_execution(tmp_path):
    from types import SimpleNamespace
    from test_affected_selection import observation
    from test_survival.policy.affected import Decisions, records_complete
    from test_survival.selection import executed
    from test_survival.policy.segments import execution_id

    member = test_test('p')
    cfg = {'project':{'repo':str(tmp_path),'module':'.'}, 'coverage':{'universe':'production'}}
    context = observation()['dependency_record']['context']
    b = SimpleNamespace(exp=tmp_path/'policy', path=tmp_path/'baseline', groups=['g'],
                        by_index={i:{'sha':str(i)} for i in range(4)},
                        options={'set':[{'name':'reset'}]}, evaluable=lambda _:True)
    runs = SimpleNamespace(adapter=SimpleNamespace(config=cfg), initialize=lambda:None)
    decisions = Decisions(b, runs)
    decisions.selector = SimpleNamespace(context=lambda *_:context, decision=lambda *a,**kw:None)
    executed_row = executed({**member, **observation(), 'lines':[[A,1],[B,2]]},1,'1',context)
    decisions.physical = lambda *_:{key(member):executed_row}
    active = segment(2,1,[member],[])
    write_json(b.exp/'results/policy/reset/segments.json',{'g':[segment(1,0,[],[]),active]})
    write_json(b.exp/f'results/policy/runs/{execution_id(member)}/1.json',{'sha':'1','methods':[executed_row]})
    consumers = [('reset','g',active,{key(member)})]
    decisions.measure(1,consumers,boundary=True)
    for i, universe in [(2,[[A,1]]),(3,[[A,1],[B,2],[B,3]])]:
        write_json(b.path/f'results/raw-coverage/{i}-g.json', {'universe':universe})
        row = decisions.measure(i,consumers)['reset','g'][0]
        assert row['lines'] == [line for line in executed_row['lines'] if line in universe]
        assert row['carried_from_index'] == 1
    assert records_complete(b)


def test_scope_and_production_batch_caches_cannot_cross(tmp_path, monkeypatch):
    config = fixture(tmp_path)
    jobs = []
    setup_worker(monkeypatch,jobs)
    control.run(tmp_path,config)
    scope_record = read_json(tmp_path/'results/baseline-batches/0.json')
    config['coverage']['universe'] = 'production'
    assert not batch.stage_complete(tmp_path,config,'control')
    control.run(tmp_path,config)
    assert len(jobs) == 2
    assert batch.stage_complete(tmp_path,config,'control')
    assert scope_record['input_sha256'] != read_json(tmp_path/'results/baseline-batches/0.json')['input_sha256']
    config['coverage'].pop('universe')
    assert not batch.stage_complete(tmp_path,config,'control')
    control.run(tmp_path,config)
    assert read_json(tmp_path/'results/baseline-batches/0.json') == scope_record


def test_switch_before_control_reuses_completed_stages(tmp_path):
    from test_survival.pipeline import run
    config = validate(dict(name='switch', anchor='t', project={'repo':'.'}, survival={'execution':'batch'}))
    handlers = {s:lambda *args:'fixture' for s in STAGES}
    assert run(tmp_path, config, until='screen', handlers=handlers, no_panel=True) == list(STAGES[:4])
    states = {s:(tmp_path/f'state/{s}.json').read_bytes() for s in STAGES[:4]}
    config['coverage']['universe'] = 'production'
    assert run(tmp_path, config, until='control', handlers=handlers, no_panel=True) == ['control']
    assert all((tmp_path/f'state/{s}.json').read_bytes() == data for s,data in states.items())


@pytest.mark.parametrize('production', [False, True])
def test_regenerated_run_retains_selected_universe(baseline, monkeypatch, production):
    from test_survival.policy.execute import Runs
    from test_survival.policy.pipeline import Steps
    from test_survival.policy.plan import build_plan
    exp, cfg, _ = baseline
    b = Baseline(exp,cfg)
    if production:
        b.execution_config['coverage']['universe'] = 'production'
    runs = Runs(b, Steps(b,build_plan(b)))
    test = test_test('regenerated')
    runs.register([test])
    monkeypatch.setattr(runs, 'initialize', lambda:None)
    monkeypatch.setattr(runs.adapter, 'build', lambda *_:{'build':'ok'})
    monkeypatch.setattr(runs.adapter, 'worker', lambda job:{'methods':[dict(test,state='pass',lines=[[B,2]])]})
    runs.run_index(3, [test['file_hash']])
    row = read_json(runs.path(3, test['file_hash']))['methods'][0]
    assert row['lines'] == ([[B,2]] if production else [])
