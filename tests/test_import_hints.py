"""Prompt compatibility and pre-call checks using only temporary fixtures."""
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import STAGES, as_written, stage_hash, stage_inputs, validate
from test_survival.generators.parts import context, import_hints, prompts
from test_survival.io import atomic_text, read_json, write_json
from test_survival.model.openrouter import OpenRouter
from test_survival.policy.config import digest_options
from test_survival.policy.pools import pool_config
from test_survival.stages import generate

SOURCE = 'package p; class Owner {}'
SIGNATURE = '// public API of q.Api\npublic class Api {\n}\n'
# SHA-256 of all five rendered prompts for this fixture at 71ece8f.
LEGACY = {
    'ZSL': '84004853d9a803fbccc2332f33fd9bd677e44050d236525dd5d0cd1d11ecfd77',
    'FSL': 'e623f3245657cb22be4adaab6b668221d2e766355dd6c21aae534c1f975ea612',
    'CoT': 'dc2137d97f2d316fcbe17c4b35aada64bc04fb49a7908261a513329ea86a9d5f',
    'ToT': '3879ee10a39c76717e1714b95c153a9c35e9eefe3b4eb2fad4dcd8cfa638925a',
    'GToT': '4fc474511726b0f21697b4a4a53dba742d1b6baff493adc5bf45456a276be59b',
}
LIMITS = dict(max_types=15, max_lines=400)


def inventory():
    types = {fqn: dict(package=pkg, modifiers='public', kind='CLASS', parameters='', extends='', implements='')
             for pkg, names in [('p', ['Owner']), ('q', ['Ast', 'Ast.Literal', 'Core', 'Api']),
                                ('other', ['Ast', 'Ast.Op'])]
             for fqn in (pkg+'.'+name for name in names)}
    refs = defaultdict(lambda: defaultdict(list))
    refs['p.Owner']['source-import'] = ['q.Ast.Literal', 'q.Core', 'other.Ast.Op']
    refs['q.Core']['signature-type'] = ['q.Api']
    return types, {'p.Owner': ['q.Core']}, refs, defaultdict(list)


def test_final_block_and_default_off_prompt_bytes():
    block, meta = import_hints.render(inventory(), 'p.Owner', ['q.Core'], LIMITS)
    import_hints.gate(block, meta, LIMITS)
    assert block.splitlines()[3:-5] == [
        'import p.Owner;', 'import q.Ast;', '// Use Ast.Literal.', 'import q.Core;',
        '// Use other.Ast.Op.', 'import q.Api;',
    ]
    for technique, digest in LEGACY.items():
        original = prompts.render(technique, 'Owner', SOURCE, SIGNATURE, True)
        assert sha256(original.encode()).hexdigest() == digest
        hinted = prompts.render(technique, 'Owner', SOURCE, SIGNATURE, True, block)
        assert hinted.count(block) == 1
        assert hinted == original.replace(SOURCE+'\n\n'+prompts.CONSTRAINT+'\n\n'+SIGNATURE,
                                          SOURCE+'\n\n'+prompts.CONSTRAINT+'\n\n'+SIGNATURE+'\n\n'+block)


def test_config_hash_boundaries():
    path = Path(__file__).parents[1]/'examples/jsv-run2/experiment.yaml'
    raw = yaml.safe_load(path.read_text())
    before = validate(raw)
    for group in raw['generators']:
        if group['method'] == 'ouedraogo':
            group.setdefault('prompt', {})['import_hints'] = False
    off = validate(raw)
    for stage in STAGES:
        assert stage_hash(before, stage) == stage_hash(off, stage) == stage_hash(raw, stage)
    for config in (before, off):
        # Policy regeneration expands defaults before applying overrides.
        expanded = as_written(config, include_defaults=True)
        assert all('import_hints' not in g['prompt'] for g in stage_inputs(expanded, 'generate')['generators'])
    selected = next(g for g in raw['generators'] if g['method'] == 'ouedraogo')
    selected['prompt']['import_hints'] = True
    on = validate(raw)
    assert [s for s in STAGES if stage_hash(before, s) != stage_hash(on, s)] == list(STAGES[2:])
    assert [g['name'] for g in on['generators'] if stage_hash(before, 'generate', g['name']) !=
            stage_hash(on, 'generate', g['name'])] == [selected['name']]
    options = dict(set=[], generator_overrides={})
    assert digest_options({**options, 'generator_overrides': {'o': {'prompt': {'import_hints': True}}}}) == options


@pytest.mark.parametrize('limits,kept', [(dict(max_types=2, max_lines=400), 2),
                                          (dict(max_types=15, max_lines=10), 1)])
def test_caps_keep_whole_type_hints_and_record_remaining_types(limits, kept):
    data = inventory()
    full = import_hints.render(data, 'p.Owner', ['q.Core'], LIMITS)[1]['included']
    block, meta = import_hints.render(data, 'p.Owner', ['q.Core'], limits)
    import_hints.gate(block, meta, limits)
    assert meta['included'] == full[:kept] and meta['omitted'] == full[kept:]
    assert meta['lines'] == len(block.splitlines()) <= limits['max_lines']
    assert ('import q.Ast;' in block) == ('// Use Ast.Literal.' in block)
    with pytest.raises(ValueError, match='max_lines'):
        import_hints.render(data, 'p.Owner', [], dict(max_types=1, max_lines=1))


def test_unnamed_package_library_collision_and_inaccessible_type():
    data = inventory()
    data[0]['Test'] = dict(package='', modifiers='')
    data[0]['q.Ast']['modifiers'] = ''
    data[2]['Test']['source-import'] = ['q.Ast.Literal']
    block, meta = import_hints.render(data, 'Test', [], LIMITS)
    assert import_hints.UNNAMED in block and '// Use org.junit.Test.' in block
    assert 'import Test;' not in block
    assert meta['inaccessible'] == ['q.Ast.Literal']
    import_hints.gate(block, meta, LIMITS)


def test_baseline_and_policy_requests_use_saved_block(tmp_path, monkeypatch):
    config = validate(dict(name='fixture', project={'repo': '/unused'}, anchor='v1', generators=[
        dict(name='o', method='ouedraogo', impl='native', prompt={'import_hints': True}, rounds={'max': 1})]))
    def load_inventory(adapter, directory, *, import_hints):
        assert import_hints
        return inventory()
    monkeypatch.setattr(context, 'inventory', load_inventory)
    monkeypatch.setattr(JavaMavenAdapter, 'setup_runner', lambda self: None)
    monkeypatch.setattr(generate, 'screen_round', lambda *args: ([], {'compiled': 0}))
    def complete(self, prompt, params, directory):
        write_json(directory/'request.json', self.request(prompt, params))
        return {'status': 'ok'}, ''
    monkeypatch.setattr(OpenRouter, 'complete', complete)
    point = dict(group='o', sha='v2')
    configs = [config, pool_config(SimpleNamespace(execution_config=config), point)]
    results = []
    for exp, settings in zip([tmp_path/'baseline', tmp_path/'pool'], configs):
        atomic_text(exp/'dev/dev-denominator.csv', 'id\n')
        atomic_text(exp/'work/checkouts/0/Owner.java', SOURCE)
        write_json(exp/'population/scope.json', {'classes': [{'class': 'p.Owner', 'file': 'Owner.java'}]})
        generate.run(exp, settings)
        group = exp/'generated/o'
        directory = group/'round1'
        assert read_json(group/'rounds.json')['rounds'][0]['import_hints'] is True
        assert read_json(directory/'prompts/pre-call-check.json')['import_hints_checked'] == ['Owner']
        folder = directory/'prompts/Owner'
        block = (folder/'import-hints.txt').read_text()
        assert read_json(folder/'import-hints.json')['included'] == ['p.Owner', 'q.Ast.Literal', 'q.Core', 'other.Ast.Op']
        requests = {}
        for technique in LEGACY:
            saved = read_json(directory/'runs/Owner'/technique/'request.json')['messages'][0]['content']
            assert saved == (folder/(technique+'.txt')).read_text()
            assert saved.count(block) == 1 and SOURCE+'\n\n'+block in saved
            requests[technique] = saved
        results.append(requests)
    assert results[0] == results[1]

    # Break the last class in a new round. Even the valid first class must have zero calls.
    exp = tmp_path/'bad'
    atomic_text(exp/'dev/dev-denominator.csv', 'id\n')
    rows = [{'class': 'p.Owner', 'file': 'Owner.java'}, {'class': 'p.Bad', 'file': 'Bad.java'}]
    write_json(exp/'population/scope.json', {'classes': rows})
    for row in rows:
        atomic_text(exp/'work/checkouts/0'/row['file'], 'package p; class '+row['class'][2:]+' {}')
    render = import_hints.render
    def malformed(data, target, deps, limits):
        block, meta = render(data, target, deps, limits)
        return block.replace('import p.Bad;', 'import p.;'), meta
    monkeypatch.setattr(import_hints, 'render', malformed)
    with pytest.raises(ValueError, match='Malformed import hint'):
        generate.run(exp, config)
    assert not (exp/'generated/o/round1/runs').exists()
