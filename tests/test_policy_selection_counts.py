"""Policy accounting for read-only baselines predating selection metadata."""
from copy import deepcopy

import pytest

from test_policy import baseline
from test_survival.io import read_json, read_csv, write_json
from test_survival.policy import execute
from test_survival.policy.baseline import Baseline
from test_survival.policy.pipeline import Steps
from test_survival.policy.plan import build_plan


@pytest.mark.parametrize('selection',['all','affected'])
@pytest.mark.parametrize('first_action',[None,'executed','carried'])
@pytest.mark.parametrize('skip_first',[False,True])
def test_policy_counts_legacy_baseline_and_repairs_completed_totals(baseline,monkeypatch,selection,first_action,skip_first):
    exp,config,root=baseline
    config['survival']['selection']=selection
    config['policies']['regenerate_developer']=False
    config['policies']['set']=[dict(name='keep',trigger='release',action='reset',developer='keep')]
    for index in (1,2,3):
        path=root/f'results/raw/{index}-dev-scope.json';raw=read_json(path)
        raw['methods'][0]['lines']=[]
        if index==1 and first_action:
            raw['methods'][0]['selection_action']=first_action
            if first_action=='carried':raw['methods'][0]['carried_from_index']=0
        write_json(path,raw)
    b=Baseline(exp,config)
    if skip_first:b.skipped[1]='unevaluated fixture'
    plan=build_plan(b);steps=Steps(b,plan)
    def unexpected(*args,**kwargs):raise AssertionError('Baseline-only accounting must not execute tests')
    monkeypatch.setattr(execute.Runs,'initialize',unexpected)
    before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    folder=exp/'results/policy/keep'
    expected=[dict(index=2,group='dev-scope',executed=1,carried=0)]
    if not skip_first:expected.insert(0,dict(index=1,group='dev-scope',executed=int(first_action!='carried'),carried=int(first_action=='carried')))
    totals={action:sum(r[action] for r in expected) for action in ('executed','carried')}
    calls=[]
    def run():
        calls.append(True)
        execute.survive(b,plan,{},steps)
    steps('survive',run,outputs=[folder/'survival.csv'])
    assert read_json(folder/'selection-counts.json')==expected
    assert read_json(folder/'final-summary.json')['selection_totals']=={'dev-scope':totals}
    if selection=='affected':
        assert [{k:v for k,v in row.items() if k!='event'} for row in read_json(folder/'selection-phases.json') if row['executed'] or row['carried']]==expected
        progress=read_json(exp/'results/policy/selection-progress.json')
        assert {a:progress[a] for a in totals}==totals
    assert read_csv(folder/'first-failures.csv')[0]['index']=='2'
    tables={p:p.read_bytes() for p in (folder/'survival.csv',folder/'first-failures.csv')}
    # Resume an old completed aggregate, retaining pools/physical evidence.
    state=exp/'state/policy-survive.json';old=read_json(state);old['revision']['survive']=3;write_json(state,old)
    write_json(folder/'selection-counts.json',[])
    write_json(folder/'final-summary.json',dict(selection_totals={'dev-scope':dict(executed=0,carried=0)}))
    retained=exp/'results/policy/pools/fixture.json';write_json(retained,dict(frozen=True));retained_bytes=retained.read_bytes()
    steps('survive',run,outputs=[folder/'survival.csv'])
    assert len(calls)==2
    assert read_json(folder/'selection-counts.json')==expected
    assert read_json(folder/'final-summary.json')['selection_totals']=={'dev-scope':totals}
    steps('survive',run,outputs=[folder/'survival.csv']);assert len(calls)==2
    assert retained.read_bytes()==retained_bytes
    assert tables=={p:p.read_bytes() for p in tables}
    assert before=={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_baseline_observations_tag_only_actual_legacy_states():
    from types import SimpleNamespace
    from test_survival.policy.segments import key
    states=['pass','fail','compile-fail','error','timeout','absent','dead','not-selected']
    tests=[dict(id=state,origin_id=state,file_hash='snapshot',execution_hash='snapshot',**{'class':'Test'},method=state) for state in states]
    raw=dict(methods=[dict(id=state,state=state) for state in states])
    original=deepcopy(raw)
    rows=execute.baseline_observations(SimpleNamespace(raw=lambda *args:raw),2,'dev',tests)
    assert [row['id'] for row in rows]==[key(t) for t in tests]
    assert [row.get('selection_action') for row in rows]==['executed']*6+[None,None]
    assert all('dependency_record' not in row and 'last_executed_index' not in row for row in rows)
    assert raw==original


@pytest.mark.parametrize('source',['seeded','cached'])
def test_regenerated_fill_counts_untagged_reused_physical_rows(baseline,monkeypatch,source):
    from test_survival.policy.config import COVERAGE_VERSION
    from test_survival.policy.segments import execution_id
    exp,config,root=baseline
    config['policies']['set']=[dict(name='fill',trigger='release',action='fill',developer='regenerate')]
    b=Baseline(exp,config)
    for commit in b.curves:commit['dev-scope_alive']='1';commit['dev-scope_lost']='0'
    plan=build_plan(b);steps=Steps(b,plan);frozen=execute.baseline_tests(b)['dev-scope'];test=frozen[0]
    for index in (1,2,3):
        row=dict(test,id=test['origin_id'],state='pass',lines=[])
        write_json(root/f'results/raw/{index}-dev-scope.json',dict(sha=b.by_index[index]['sha'],execution='batch',coverage_version=COVERAGE_VERSION,methods=[row]))
    if source=='cached':
        for index in (2,3):
            path=exp/f'results/policy/runs/{execution_id(test)}/{index}.json'
            steps(f'run-{execution_id(test)}-{index}',lambda path=path,index=index:write_json(path,dict(sha=b.by_index[index]['sha'],coverage_version=COVERAGE_VERSION,
                    coverage_status='complete',methods=[dict(test,state='pass',lines=[])])),outputs=[path])
    before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    def unexpected(*args,**kwargs):raise AssertionError('Reusable evidence must not run Java')
    monkeypatch.setattr(execute.Runs,'initialize',unexpected)
    run=lambda:execute.survive(b,plan,{('dev-scope',2):frozen},steps)
    out=exp/'results/policy/fill'
    steps('survive',run,outputs=[out/'survival.csv'])
    assert read_json(out/'selection-counts.json')==[dict(index=i,group='dev-scope',executed=1,carried=0) for i in (1,2,3)]
    assert read_json(out/'final-summary.json')['selection_totals']['dev-scope']==dict(executed=3,carried=0)
    assert not read_csv(out/'first-failures.csv')
    saved={p:p.read_bytes() for p in (exp/'results/policy/runs').rglob('*') if p.is_file()}
    state=exp/'state/policy-survive.json';old=read_json(state);old['revision']['survive']=4;write_json(state,old)
    write_json(out/'selection-counts.json',[])
    steps('survive',run,outputs=[out/'survival.csv'])
    assert [r['executed'] for r in read_json(out/'selection-counts.json')]==[1,1,1]
    assert saved=={p:p.read_bytes() for p in saved}
    assert before=={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('selection',['all','affected'])
def test_policy_accounting_does_not_rescan_prior_indices(baseline,monkeypatch,selection):
    from test_survival.policy import affected
    from test_survival.selection import counts
    exp,config,root=baseline
    config['survival']['selection']=selection
    config['policies']['set']=[dict(name='keep',trigger='release',action='reset',developer='keep')]
    b=Baseline(exp,config);anchor=deepcopy(b.curves[0]);template=b.curves[1]
    b.curves=[anchor]+[dict(template,index=str(i),sha=f'commit{i}',**{'dev-scope_alive':'1','dev-scope_lost':'0'}) for i in range(1,31)]
    b.by_index={int(c['index']):c for c in b.curves}
    for index in range(1,31):
        write_json(root/f'results/raw/{index}-dev-scope.json',dict(methods=[dict(b.frozen['dev-scope'][0],state='pass',lines=[],selection_action='executed')]))
    visits=[]
    for module in (execute,affected):
        original=getattr(module,'counts',counts)
        def counted(rows,original=original):
            rows=list(rows);visits.extend([1]*len(rows));return original(rows)
        monkeypatch.setattr(module,'counts',counted,raising=False)
    plan=dict(points=[],pools=[]);execute.survive(b,plan,{},Steps(b,plan))
    assert sum(visits)<=3*30
    assert read_json(exp/'results/policy/keep/final-summary.json')['selection_totals']['dev-scope']==dict(executed=30,carried=0)


def test_incremental_ledger_deduplicates_boundary_phases_and_discards_old_rows():
    from test_survival.policy.accounting import SelectionLedger
    ledger=SelectionLedger()
    legacy=dict(id='legacy',state='pass')
    carried=dict(id='same',state='pass',selection_action='carried')
    ledger.record(1,'g',[legacy,carried,dict(id='dead',state='dead'),dict(id='unused',state='not-selected')])
    assert ledger.completed==dict(executed=1,carried=1)
    ledger.record(1,'g',[dict(carried,selection_action='executed'),legacy])
    ledger.record(1,'g',[carried])  # Execution at the boundary wins over carrying.
    ledger.record(1,'other',[carried])
    assert ledger.completed==dict(executed=2,carried=1)
    ledger.record(2,'g',[carried])
    assert len(ledger.current)==1
    assert ledger.rows()==[dict(index=1,group='g',executed=2,carried=0),dict(index=1,group='other',executed=0,carried=1),dict(index=2,group='g',executed=0,carried=1)]
    assert ledger.totals(['g','other','empty'])==dict(g=dict(executed=2,carried=1),other=dict(executed=0,carried=1),empty=dict(executed=0,carried=0))
    assert 'selection_action' not in legacy
    with pytest.raises(ValueError,match='ordered'):ledger.record(1,'g',[])


def test_incremental_ledger_work_is_linear_in_observations():
    from test_survival.policy.accounting import SelectionLedger
    class Row(dict):
        reads=0
        def get(self,*args):
            type(self).reads+=1
            return super().get(*args)
    rows=[Row(id=str(n),state='pass',selection_action='carried') for n in range(1000)]
    ledger=SelectionLedger()
    for index in range(600):
        ledger.record(index,'g',iter(rows))
        assert ledger.completed==dict(executed=0,carried=(index+1)*1000)
        assert len(ledger.current)==1000
    assert Row.reads==2*600*1000
    assert len(ledger.rows())==600
    assert Row.reads==2*600*1000  # Final projection never revisits method rows.
