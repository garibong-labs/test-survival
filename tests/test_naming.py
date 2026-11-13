from test_survival.io import write_json
from test_survival.policy.artifacts import unit
from test_survival.rules.naming import folder_keys


SCOPE = [{'class': 'a.Converter'}, {'class': 'a.b.Converter'}, {'class': 'a.Other'}]


def test_folder_keys_use_full_name_only_for_shared_simple_names():
    assert folder_keys([c['class'] for c in SCOPE]) == {
        'a.Converter': 'a.Converter', 'a.b.Converter': 'a.b.Converter', 'a.Other': 'Other'}


def test_ouedraogo_unit_uses_recorded_target_class(tmp_path):
    folder = tmp_path/'generated/ouedraogo/round1'
    folder.mkdir(parents=True)
    file = 'generated/ouedraogo/round1/sources/a.b.Converter/ZSL/ConverterTest.java'
    write_json(folder/'generation.json', [dict(class_name='a.b.Converter', target_class='a.b.Converter', file=file)])
    assert unit(tmp_path, 'ouedraogo', {'file': file}, 'ouedraogo', SCOPE) == 'a.b.Converter'


def test_ouedraogo_unit_maps_full_name_folder_without_ledger(tmp_path):
    file = 'generated/ouedraogo/round1/sources/a.Converter/ZSL/ConverterTest.java'
    assert unit(tmp_path, 'ouedraogo', {'file': file}, 'ouedraogo', SCOPE) == 'a.Converter'


def test_ouedraogo_unit_keeps_simple_name_mapping(tmp_path):
    file = 'generated/ouedraogo/round1/sources/Other/ZSL/OtherTest.java'
    assert unit(tmp_path, 'ouedraogo', {'file': file}, 'ouedraogo', SCOPE) == 'a.Other'
