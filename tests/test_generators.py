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
    gate('class A {\n}')
    gate('interface ExecutionCustomizer {\n}')
    with pytest.raises(ValueError):gate('nonsense class A {\n}')


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
        def setup_runner(self):calls.append('setup')
        def generate(self,n):
            assert calls[0]=='setup'
            calls.append(n);return SimpleNamespace(calls=[])
    monkeypatch.setattr(java_maven,'JavaMavenAdapter',Fake)
    monkeypatch.setattr(generate,'OuedraogoNative',Fake)
    monkeypatch.setattr(generate,'screen_round',lambda *args:([],{'compiled':0}))
    generate.run(tmp_path,config)
    record=read_json(tmp_path/'generated/o/rounds.json')
    assert calls==['setup',1,2] and record['stopped']=='zero-compile'
    assert [r['directory'] for r in record['rounds']]==['round1','round2']


@pytest.mark.parametrize('manifest', [True, False])
def test_failed_generate_reuses_ktester_and_25_ouedraogo_responses(tmp_path, monkeypatch, manifest):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.config import STAGES, legacy_stage_hash, stage_hash, validate
    from test_survival.generators.ouedraogo_native import OuedraogoNative
    from test_survival.generators.ktester_tool import KTesterTool
    from test_survival.generators.parts import context, postprocess
    from test_survival.io import atomic_text, read_json, write_json, write_csv
    from test_survival.model.openrouter import OpenRouter
    from test_survival.pipeline import run
    from test_survival.stages import generate
    config = validate(dict(name='resume', project={'repo': '/unused'}, anchor='t',
                           generators=[dict(name='ktester', method='ktester', impl='author-tool', tool_path='/unused'),
                                       dict(name='ouedraogo', method='ouedraogo', impl='native', rounds={'max': 1},
                                            postprocess={'import_repair': {'enabled': True}})]))
    group = config['generators'][1]
    directory = tmp_path/'generated/ouedraogo/round1'
    scope, records = [], []
    for number in range(5):
        name = f'C{number}'
        source = f'public class {name} {{}}'
        source_path = f'src/main/java/{name}.java'
        atomic_text(tmp_path/'work/checkouts/0'/source_path, source)
        scope.append({'class': name, 'file': source_path})
        for technique in group['params']['prompts']:
            prompt = render(technique, name, source, '', group['prompt']['api_constraint'])
            write_json(directory/'runs'/name/technique/'request.json',
                       dict(model=config['model']['name'], temperature=group['params']['temperature'],
                            max_tokens=group['params']['max_tokens'], messages=[dict(role='user', content=prompt)]))
            file = directory/'sources'/name/technique/f'{name}Test.java'
            atomic_text(file, f'public class {name}Test {{}}')
            record = dict(class_name=name, technique=technique, status='ok', file=str(file.relative_to(tmp_path)), calls=[{'status': 'ok'}])
            records.append(record)
            write_json(directory/'records'/f'{name}-{technique}.json', record)
            write_json(directory/'raw-responses'/f'{name}-{technique}.json', {'id': f'{name}-{technique}'})
    assert len(records) == 25
    if manifest:
        write_json(directory/'generation.json', records)
    write_json(tmp_path/'population/scope.json', {'classes': scope})
    write_json(tmp_path/'generated/ktester/rounds.json',
               {'config_hash': legacy_stage_hash(config, 'generate', 'ktester'), 'stopped': 'max-rounds',
                'rounds': [{'round': 1, 'funnel': {'methods': 507, 'stable_3_of_3': 5}}]})
    write_csv(tmp_path/'dev/dev-denominator.csv', [], ['class', 'method', 'kind'])
    for stage in STAGES[:2]:
        write_json(tmp_path/f'state/{stage}.json', {'status': 'completed', 'config_hash': stage_hash(config, stage)})
    write_json(tmp_path/'state/generate.json', {'status': 'failed', 'config_hash': stage_hash(config, 'generate')})
    preserved = [tmp_path/'generated/ktester/rounds.json', *sorted((directory/'raw-responses').glob('*.json'))]
    before = {p: p.read_bytes() for p in preserved}
    monkeypatch.setattr(JavaMavenAdapter, 'setup_runner', lambda self: None)
    monkeypatch.setattr(context, 'inventory', lambda *args: None)
    def unexpected(*args):
        pytest.fail('Recorded model work must not be called again')
    monkeypatch.setattr(OpenRouter, 'complete', unexpected)
    monkeypatch.setattr(KTesterTool, 'generate', unexpected)
    repairs = []
    monkeypatch.setattr(postprocess, 'repair', lambda adapter, path, passes: repairs.append(path))
    screened = []
    def screen(exp, config, group, path, prior):
        assert repairs == [directory]
        screened.append(group['name'])
        return [], {'compiled': 25}
    monkeypatch.setattr(generate, 'screen_round', screen)
    run(tmp_path, config, until='generate', no_panel=True)
    assert repairs == [directory] and screened == ['ouedraogo']
    assert {p: p.read_bytes() for p in preserved} == before
    assert read_json(tmp_path/'state/generate.json')['status'] == 'completed'
    assert read_json(directory.parent/'rounds.json')['rounds'][0]['model_calls'] == 25
    # Recorded requests must still reject changed model parameters on resume.
    changed = {**group, 'params': {**group['params'], 'temperature': 0.123}}
    with pytest.raises(RuntimeError, match='different request parameters'):
        OuedraogoNative(tmp_path, config, changed).generate(1)
