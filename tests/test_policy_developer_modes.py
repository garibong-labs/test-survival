from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_policy import baseline
from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import ConfigError, validate
from test_survival.io import read_json, read_csv, write_json
from test_survival.policy.baseline import Baseline
from test_survival.policy import pools, execute, cover, report
from test_survival.policy.config import regenerates, digest_options, digest_plan
from test_survival.policy.pipeline import Steps
from test_survival.policy.plan import build_plan, run as plan


def mixed(config):
    config['policies']['regenerate_developer']=False
    config['policies']['set']=[dict(name='release-reset',trigger='release',action='reset',developer='regenerate'),
                               dict(name='half-reset',trigger='threshold',action='reset',developer='regenerate'),
                               dict(name='half-fill',trigger='threshold',action='fill',developer='keep')]
    return config


def test_target_configuration_and_fallback(baseline):
    _,config,_=baseline
    changed=mixed(config);changed['survival']['execution']='batch'
    changed['dev']['stable_runs']=changed['screen']['stable_runs']=1
    result=validate(changed)
    b=Baseline(baseline[0],result);p=build_plan(b)
    assert len(p['pools'])==1 and {x['trigger'] for x in p['points']}=={'release','threshold'}
    assert regenerates(b,'dev-scope','release-reset') and regenerates(b,'dev-scope','half-reset')
    assert not regenerates(b,'dev-scope','half-fill')
    for policy in result['policies']['set']:policy.pop('developer')
    assert not regenerates(Baseline(baseline[0],result),'dev-scope')
    result['policies']['set'][0]['developer']='invalid'
    with pytest.raises(ConfigError,match='developer'):validate(result)


def test_developer_pool_opens_and_executes_segment_with_keep_reference(baseline,monkeypatch):
    exp,config,_=baseline;b=Baseline(exp,mixed(config));p=build_plan(b)
    write_json(exp/'state/run.json',dict(status='running',run_id='fixture'))
    jobs=[]
    def build(self,index,sha):
        folder=self.folder(index);source=folder/'src/test/java/ATest.java';source.parent.mkdir(parents=True,exist_ok=True)
        source.write_text('class ATest {}')
        (source.parent/'Helper.java').write_text('class Helper {}')
        write_json(self.exp/f'results/builds/{index}.json',dict(sha=sha,build='ok'))
        return dict(build='ok')
    monkeypatch.setattr(JavaMavenAdapter,'build',build)
    monkeypatch.setattr(JavaMavenAdapter,'setup_runner',lambda self:None)
    monkeypatch.setattr(JavaMavenAdapter,'list_production_methods',lambda self,i:dict(classes={'A':{}},methods=[dict(eligible=True,**{'class':'A'},method='value',signature='value()',line=1)]))
    monkeypatch.setattr(JavaMavenAdapter,'list_test_methods',lambda *a:dict(methods=[dict(kind='plain',**{'class':'ATest'},method=n,body_hash=n) for n in ('works','newMethod')]))
    def worker(self,job):
        jobs.append(deepcopy(job))
        return dict(index=job['index'],sha=job['sha'],status='evaluated',universe=[['src/main/java/A.java',1]],
                    methods=[dict(t,state='pass',flaky=False,lines=[['src/main/java/A.java',1]]) for t in job['methods']])
    monkeypatch.setattr(JavaMavenAdapter,'worker',worker)
    built=pools.run(b,p,Steps(b,p))
    assert len(built['dev-scope',2])==2
    assert any(j['action']=='policy-cover' and j['full_suite'] for j in jobs)
    assert any(j['phase']=='policy-screen' and j['repeats']==3 for j in jobs if 'phase' in j)
    assert (exp/'results/policy/pools/dev-scope/2/pool.json').exists()
    execute.survive(b,p,built,Steps(b,p));cover.run(b,None);report.run(b)
    for name in ('release-reset','half-reset'):
        assert len(read_json(exp/f'results/policy/{name}/segments.json')['dev-scope'])==2
        assert read_csv(exp/f'results/policy/{name}/survival.csv')[-1]['dev-scope_alive']=='2'
    assert len(read_json(exp/'results/policy/half-fill/segments.json')['dev-scope'])==1
    assert read_csv(exp/'results/policy/half-fill/survival.csv')[-1]['dev-scope_alive']=='0'
    assert {j['index'] for j in jobs if j['action']=='policy-survive'}=={2,3}
    summary=(exp/'results/policy/summary.md').read_text()
    assert '| half-fill | dev-scope (baseline reference) |' in summary
    assert '| release-reset | dev-scope |' in summary
    count=len(jobs);pools.run(b,p,Steps(b,p));execute.survive(b,p,built,Steps(b,p))
    assert len(jobs)==count


def test_per_policy_switch_preserves_pool_digest_and_plan_bytes(baseline):
    exp,config,_=baseline
    saved=deepcopy(plan(exp,config));before=(exp/'results/policy/plan.json').read_bytes();old=Steps(Baseline(exp,config),saved)
    config['policies']['set'][0]['developer']='keep'
    b=Baseline(exp,config)
    assert plan(exp,config)==saved and (exp/'results/policy/plan.json').read_bytes()==before
    assert Steps(b,saved).digest==old.digest


def test_enabling_regeneration_requires_missing_points_to_be_planned(baseline):
    exp,config,_=baseline;config['policies']['regenerate_developer']=False
    plan(exp,config);config['policies']['set'][0]['developer']='regenerate'
    with pytest.raises(ConfigError,match='lacks requested regeneration points'):plan(exp,config)


def test_matching_uses_largest_request_and_same_index_pool(baseline):
    exp,config,_=baseline;b=Baseline(exp,mixed(config))
    # Both threshold policies request a generator pool after a prior developer
    # reset. Keep requests its fixed denominator; regenerate requests the pool.
    b.frozen['dev-scope']=[{}]*5
    p=dict(points=[dict(group='dev-scope',index=1,trigger='threshold'),dict(group='llm',index=2,trigger='threshold')])
    assert pools.matching_target(b,p,'llm',2,{('dev-scope',1):[{}]*2})==5
    assert pools.matching_target(b,p,'llm',2,{('dev-scope',1):[{}]*8})==8
    assert pools.matching_target(b,p,'llm',2,{('dev-scope',2):[{}]*3})==3


def test_terminal_checkpoint_is_recorded_without_pool(baseline):
    exp,config,_=baseline;b=Baseline(exp,config)
    b.by_index[2]['evaluated']='no'
    p=build_plan(b)
    assert not p['pools']
    assert all(x['index'] is None and x['reason']=='no later evaluable commit' for x in p['points'])
