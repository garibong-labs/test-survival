"""Archived rounds remain evidence when the configured active cap is lowered."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import shutil

import pytest

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import STAGES, stage_hash, validate
from test_survival.generators.ktester_tool import KTesterTool
from test_survival.generators.parts.prompts import render
from test_survival.importer import rounds
from test_survival.io import atomic_text, read_csv, read_json, write_csv, write_json
from test_survival.model.openrouter import OpenRouter
from test_survival.pipeline import _run
from test_survival.stages import generate, screen


def snapshot(root):
    return {str(p.relative_to(root)):(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
            for p in root.rglob('*') if p.is_file()}


@pytest.fixture
def archived(tmp_path, monkeypatch):
    c = validate(dict(name='round-limits', anchor='t', project={'repo':str(tmp_path)},
                      screen={'exclude':{}}, generators=[
                          dict(name='ktester', method='ktester', tool_path='/unused'),
                          dict(name='ouedraogo', method='ouedraogo', impl='native',
                               params={'prompts':['ZSL']}, rounds={'max':2})]))
    g = c['generators'][1]
    write_csv(tmp_path/'dev/dev-denominator.csv', [], ['class','method','kind'])
    write_json(tmp_path/'results/anchor.json', dict(sha='t', date='date'))
    write_json(tmp_path/'population/scope.json', {'m':1, 'classes':[{'class':'A','file':'src/main/java/A.java','m':1}]})
    atomic_text(JavaMavenAdapter(tmp_path,c).folder(0)/'src/main/java/A.java', 'class A {}')
    write_json(tmp_path/'generated/ktester/rounds.json',
               dict(config_hash=stage_hash(c,'generate','ktester'), stopped='max-rounds', rounds=[]))
    for n in (1,2):
        directory=tmp_path/f'generated/ouedraogo/round{n}'
        source=directory/f'sources/A/ZSL/Test{n}.java'; atomic_text(source, f'class Test{n} {{}}')
        file=str(source.relative_to(tmp_path))
        row=dict(file=file, **{'class':f'Test{n}'}, method='works', kind='plain', body_hash=str(n),
                 state='pass', id=file+f'|Test{n}|works', round=f'round{n}', technique='ZSL')
        write_json(directory/'validation/0.json', dict(file=file, compile={'exit_code':0}, methods=[row]))
        write_json(directory/'generation.json', [dict(file=file, class_name='A', target_class='A',
                   technique='ZSL', calls=[{'status':'ok'}], status='ok')])
        prompt=render('ZSL', 'A', 'class A {}', '', g['prompt']['api_constraint'])
        write_json(directory/'runs/A/ZSL/request.json', OpenRouter(c['model']).request(prompt,g['params']))
        write_json(directory/'runs/A/ZSL/run.json', {'status':'ok'})
    def unexpected(*a, **kw):pytest.fail('Archived rounds must not call models, access credentials or launch workers')
    monkeypatch.setattr(OpenRouter, 'complete', unexpected)
    monkeypatch.setattr(KTesterTool, 'generate', unexpected)
    from test_survival.model import credentials, openrouter
    from test_survival.generators import ktester_tool
    monkeypatch.setattr(credentials, '_read', unexpected)
    monkeypatch.setattr(openrouter, '_read', unexpected)
    monkeypatch.setattr(ktester_tool, '_read', unexpected)
    monkeypatch.setattr(JavaMavenAdapter, 'worker', unexpected)
    monkeypatch.setattr(JavaMavenAdapter, 'setup_runner', lambda *a:None)
    screened=[]
    def saved_screen(self, group, label, source, repeats, number):
        screened.append(label)
        return read_json(source.parents[3]/f'validation/{number}.json')
    monkeypatch.setattr(JavaMavenAdapter, 'screen_file', saved_screen)
    _run(tmp_path,c,selected=['generate','screen'])
    screened.clear()
    return tmp_path,c,screened


def test_lower_and_raise_reuse_archives_without_calls_or_credentials(archived):
    exp,old,screened=archived
    c=deepcopy(old);c['generators'][1]['rounds']['max']=1
    assert [stage_hash(old,s)!=stage_hash(c,s) for s in STAGES] == [False,False,True,True,True,True,True,True]
    assert stage_hash(old,'generate','ktester')==stage_hash(c,'generate','ktester')
    assert stage_hash(old,'generate','ouedraogo')!=stage_hash(c,'generate','ouedraogo')
    higher=exp/'generated/ouedraogo/round2'; before=snapshot(higher)
    write_json(exp/'state/control.json', dict(status='failed',config_hash=stage_hash(old,'control')))
    _run(exp,c,selected=['generate','screen'])
    assert screened and set(screened)=={'round1'}
    manifest=read_json(exp/'generated/ouedraogo/rounds.json')
    assert [r['round'] for r in manifest['rounds']]==[1] and manifest['stopped']=='max-rounds'
    assert manifest['rounds'][0]['funnel']['total_kept']==1
    assert read_json(exp/'results/denominators.json')['ouedraogo']==1
    assert all('round2' not in t['file'] for t in read_json(exp/'results/frozen-denominators.json')['ouedraogo'])
    assert len(read_csv(exp/'results/llm-ouedraogo-denominator.csv'))==1
    from test_survival.progress import items, generation_progress
    assert items(exp,'screen',c,{})[1]==1
    assert generation_progress(exp,c,{})['total']==1
    from test_survival.report.consistency import build
    write_csv(exp/'results/survival.csv', [dict(index=0, dev_alive=0, ktester_alive=0, ouedraogo_alive=1)],
              ['index','dev_alive','ktester_alive','ouedraogo_alive'])
    write_json(exp/'results/survival-consistency.json', dict(missing_records=[],nonincreasing=True,raw_count_matches=True))
    write_json(exp/'results/coverage-consistency.json', dict(identical_executable_line_sets=[],coverage_indices=[],missing=[]))
    assert len(build(exp,c)['model_rounds']['ouedraogo']['rounds'])==1
    assert snapshot(higher)==before
    # Raising back to two selects the retained second archive again, still without model access.
    screened.clear();_run(exp,old,selected=['generate','screen'])
    assert set(screened)=={'round1','round2'}
    assert [r['round'] for r in read_json(exp/'generated/ouedraogo/rounds.json')['rounds']]==[1,2]
    assert read_json(exp/'results/denominators.json')['ouedraogo']==2


def test_policy_pool_funnel_and_rejection_scan_obey_cap(archived, monkeypatch):
    from test_survival.policy import pools
    from test_survival.policy.execute import screening_exclusion_details
    source,c,_=archived;c=deepcopy(c);c['generators']=c['generators'][1:];c['generators'][0]['rounds']['max']=1
    exp=source/'policy';root=exp/'results/policy/pools/ouedraogo/2'
    shutil.copytree(source/'generated',root/'generated')
    write_csv(root/'dev/dev-denominator.csv',[],['class','method','kind'])
    higher=root/'generated/ouedraogo/round2';before=snapshot(higher)
    write_json(exp/'state/run.json',dict(status='running',run_id='fixture'))
    b=SimpleNamespace(exp=exp, execution_config=c, scope={'classes':[{'class':'A','file':'src/main/java/A.java'}]},
                      by_index={2:{'date':'date'}})
    monkeypatch.setattr(JavaMavenAdapter,'build',lambda *a:{'build':'ok'})
    monkeypatch.setattr(JavaMavenAdapter,'list_production_methods',lambda *a:dict(classes={'A':{}},methods=[]))
    monkeypatch.setattr(JavaMavenAdapter,'list_test_methods',lambda *a:{'methods':[]})
    pool=pools.build_pool(b,dict(group='ouedraogo',index=2,sha='2'),0)
    assert len(pool)==1 and 'round1' in pool[0]['file']
    assert len(read_json(root/'funnel.json'))==1
    excluded,summary=screening_exclusion_details(exp,{('ouedraogo',2):pool},methods={'ouedraogo':'ouedraogo'},round_limits={'ouedraogo':1})
    assert not excluded and summary['ouedraogo/2']['rejected']==0
    assert snapshot(higher)==before


def test_round_cap_includes_retries_without_removing_archives(tmp_path):
    for name in ('round1','round1-retry','round1-retry2','round2','round2-retry'):(tmp_path/name).mkdir()
    assert [p.name for p in rounds(tmp_path,1)]==['round1','round1-retry','round1-retry2']
    assert len(rounds(tmp_path))==5
