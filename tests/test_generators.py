import pytest
from test_survival.generators.parts.extraction import extract
from test_survival.generators.parts.prompts import gate,render,CONSTRAINT
from test_survival.rules.stop import decision


def test_extraction_preserves_literals_and_detects_truncation():
    code='public class T { @Test void test() { String x="<html>{"; } }\n'
    result=extract('###Test START###\n'+code+'###Test END###\n')
    assert result['csr_v2'] and result['combine_v2']==code
    assert not extract('```java\npublic class T { @Test void test() {\n```')['csr_v2']


def test_prompt_substitution_does_not_rescan_source():
    text=render('ZSL','SomeClass','class SomeClass { /* {fewshot_example} */ }',constraint=True)
    assert '{fewshot_example}' in text and CONSTRAINT in text


def test_dependency_gate():
    gate('// public API of p.A\npublic abstract class A<T> {\n public void f(String... args);\n}\n')
    with pytest.raises(ValueError):gate('public class A<> {\n}')
    with pytest.raises(ValueError):gate('class A {\n}')


def test_round_stopping_priority_and_limits():
    c={'max':3,'until_dev_matched':True,'stop_after_zero_compile':2}
    assert decision(c,1,[0],0,185)=='next'
    assert decision(c,2,[0,0],0,185)=='zero-compile'
    assert decision(c,2,[2,2],185,185)=='dev-matched'
    assert decision(c,2,[2,2],20,185)=='next'
    assert decision(c,3,[2,2,2],20,185)=='max-rounds'


def test_zero_compile_round_loop_advances_without_retry(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from test_survival.config import validate
    from test_survival.stages import generate
    from test_survival.adapters import java_maven
    from test_survival.io import write_csv,read_json
    config=validate({'name':'test','project':{'repo':'/p'},'anchor':'t','generators':[{'name':'o','method':'ouedraogo','impl':'native','rounds':{'max':5}}]})
    write_csv(tmp_path/'dev/dev-denominator.csv',[{'class':'C','method':'t','kind':'plain'}],['class','method','kind'])
    calls=[]
    class Fake:
        def __init__(self,*args):pass
        def setup_runner(self):pass
        def generate(self,n):calls.append(n);return SimpleNamespace(calls=[])
    monkeypatch.setattr(java_maven,'JavaMavenAdapter',Fake)
    monkeypatch.setattr(generate,'OuedraogoNative',Fake)
    monkeypatch.setattr(generate,'screen_round',lambda *args:([],{'compiled':0}))
    generate.run(tmp_path,config)
    record=read_json(tmp_path/'generated/o/rounds.json')
    assert calls==[1,2] and record['stopped']=='zero-compile'
    assert [r['directory'] for r in record['rounds']]==['round1','round2']
