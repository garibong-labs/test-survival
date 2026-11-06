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
