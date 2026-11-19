import csv
import json
import sys
from types import SimpleNamespace

import pytest

from test_survival.adapters import ktester_worker
from test_survival.adapters.ktester_worker import (FocalMethod, match_methods, matching_positions,
                                                  restore_method_names, simple_key, validate_population)
from test_survival.rules.matching import renumber


def visit(line, qualified_type):
    ref = SimpleNamespace(getQualifiedName=lambda: qualified_type,
                          getSimpleName=lambda: qualified_type.replace('$', '.').rsplit('.', 1)[-1])
    return SimpleNamespace(getSimpleName=lambda: 'visit',
                           getPosition=lambda: SimpleNamespace(getLine=lambda: line),
                           getParameters=lambda: [SimpleNamespace(getType=lambda: ref)])


def extractor(*methods):
    ct = SimpleNamespace(getMethods=lambda: methods)

    def get_method(cls, name, args):
        # Match the author tool's first qualified-name suffix lookup.
        return next((m for m in cls.getMethods() if m.getSimpleName() == name and
                     m.getParameters()[0].getType().getQualifiedName().endswith(args[0].replace('.', '$'))), None)

    return SimpleNamespace(getCtClass=lambda name: ct, getCtMethod=get_method)


def index(*signatures):
    return {'visit': [{'signature': 'public void ' + s} for s in signatures]}


def match_originals(cls, eligible, spoon, indexed):
    matches = match_methods([cls], {cls: eligible}, spoon, {cls: {'methods': indexed}})
    return [m.cls + '.' + m.original for m in matches]


def test_nested_overloads_keep_their_index_spelling_and_spoon_order():
    spoon = extractor(visit(12, 'p.A$X'), visit(10, 'p.B$X'), visit(15, 'p.C$X'))
    indexed = index('visit(A.X)', 'visit(C.X)', 'visit(B.X)')
    # C.X is in the index but was ineligible in the source inventory.
    eligible = ['visit(p.A.X)', 'visit(B.X)']
    assert match_originals('p.Visitor', eligible, spoon, indexed) == [
        'p.Visitor.visit(B.X)', 'p.Visitor.visit(A.X)']


def test_nested_array_overloads_keep_the_entire_enclosing_chain():
    spoon = extractor(visit(4, 'p.Host$A$Inner$X[]'), visit(8, 'p.Host$B$Inner$X[]'))
    eligible = ['visit(B.Inner.X[])', 'visit(A.Inner.X...)']
    indexed = index('visit(B.Inner.X[])', 'visit(A.Inner.X[])')
    assert match_originals('p.Host', eligible, spoon, indexed) == [
        'p.Host.visit(A.Inner.X[])', 'p.Host.visit(B.Inner.X[])']


def test_unambiguous_method_keeps_simple_name_matching():
    assert match_originals('p.Visitor', ['visit(X<String>...)'],
                               extractor(visit(1, 'p.Outer$X[]')), index('visit(X<String>[])')) == [
                                   'p.Visitor.visit(X<String>[])']


@pytest.mark.parametrize(('eligible', 'indexed'), [
    (['visit(X)'], index('visit(A.X)', 'visit(B.X)')),
    (['visit(A.X)'], index('visit(X)')),
    (['visit(A.X)', 'visit(B.X)'], index('visit(A.X)')),
])
def test_ambiguous_or_missing_match_identifies_class_and_overloads(eligible, indexed):
    with pytest.raises(ValueError) as error:
        match_originals('p.Visitor', eligible, extractor(visit(1, 'p.A$X'), visit(2, 'p.B$X')), indexed)
    assert all(s in str(error.value) for s in ('p.Visitor', 'visit(p.A.X)', 'visit(p.B.X)'))


def test_author_lookup_cannot_silently_select_a_different_focal_body():
    spoon = extractor(visit(1, 'p.BA$X'), visit(2, 'p.A$X'))
    signatures = ['visit(BA.X)', 'visit(A.X)']
    with pytest.raises(ValueError) as error:
        match_originals('p.Visitor', signatures, spoon, index(*signatures))
    assert 'p.Visitor.visit(A.X): DatasetPrepare lookup' in str(error.value)


@pytest.mark.parametrize(('annotated', 'clean', 'simple'), [
    ('Outer.@Ann Inner', 'Outer.Inner', 'Inner'),
    ('@Ann Type', 'Type', 'Type'),
    ('List<@Ann T>', 'List', 'List'),
    ('Outer.@pkg.Ann(value = @B(text = ")"), flags = {1, 2}) Inner', 'Outer.Inner', 'Inner'),
])
def test_type_use_annotations_in_simple_and_qualified_matching(annotated, clean, simple):
    signature = f'visit({annotated})'
    qualified = {0: ('p.' + clean,), 1: ('p.Other',)}
    assert simple_key(signature) == f'visit({simple})'
    assert matching_positions(signature, qualified) == [0]


def test_lookup_substitutions_preserve_exact_overload_and_prefer_original():
    spoon = extractor(visit(1, 'p.A$X'), visit(2, 'p.B$X'), visit(3, 'java.util.List'),
                      visit(4, 'java.util.function.Function'))
    signatures = ['visit(B.@q.Ann(value = "(") X)', 'visit(A.@Ann X)', 'visit(List<@Ann T>)',
                  'visit(java.util.function.Function<? super K, ? extends V>)']
    matches = match_methods(['p.V'], {'p.V': signatures}, spoon, {'p.V': {'methods': index(*signatures)}})
    assert [(m.original, m.lookup) for m in matches] == [
        (signatures[1], 'visit(A.X)'), (signatures[0], 'visit(B.X)'),
        (signatures[2], signatures[2]), (signatures[3], 'visit(Function)')]


def test_all_unmatched_methods_are_reported_across_classes():
    eligible = {'p.A': ['visit(A.X)', 'visit(Missing)'], 'p.B': ['visit(Y)', 'visit(Z)']}
    spoon = extractor(visit(1, 'p.BA$X'), visit(2, 'p.A$X'))
    indexed = {'p.A': {'methods': index('visit(A.X)', 'visit(BA.X)')}}
    with pytest.raises(ValueError) as error:
        match_methods(eligible, eligible, spoon, indexed)
    messages = str(error.value).splitlines()[1:]
    assert len(messages) == 4
    for cls, signatures in eligible.items():
        for signature in signatures:
            assert any(cls in msg and signature in msg for msg in messages)


def test_empty_bodies_report_all_original_class_and_signature_pairs():
    matches = [FocalMethod('p.A', 'visit(@Ann X)', 'visit(X)'), FocalMethod('p.B', 'visit(Y)', 'visit(Y)')]
    rows = [{'class': 'p.B', 'method-name': 'visit(Y)', 'focal-method': ' '},
            {'class': 'p.A', 'method-name': 'visit(X)', 'focal-method': ''}]
    with pytest.raises(ValueError) as error:
        restore_method_names(rows, matches)
    assert str(error.value).splitlines()[1:] == ['p.B.visit(Y)', 'p.A.visit(@Ann X)']


@pytest.mark.parametrize('original,lookup', [('visit(Outer.@Ann X)', 'visit(Outer.X)'),
                                             ('visit(Outer.X)', 'visit(Outer.X)')])
def test_dataset_publishes_original_names_after_author_lookup(tmp_path, monkeypatch, original, lookup):
    cls = 'p.Visitor'
    population = tmp_path/'population'
    population.mkdir()
    ds = tmp_path/'dataset'
    ds.mkdir()
    (tmp_path/'json').mkdir()
    (tmp_path/'json/fixture.json').write_text(json.dumps({'source': {cls: {'methods': index(original)}}}))
    (tmp_path/'source-inventory.json').write_text(json.dumps({'main': {'methods': [
        {'class': cls, 'signature': original, 'eligible': True}]}}))
    (population/'scope.json').write_text(json.dumps({'classes': [{'class': cls}], 'm': 1}))
    for key, value in dict(EXP=tmp_path, RESULTS=tmp_path, DS=ds, PROJECT=tmp_path, INDEX=tmp_path,
                           PROJECT_NAME='fixture').items():
        monkeypatch.setattr(ktester_worker, key, value, raising=False)

    def read_json(path):
        return json.loads(path.read_text())

    row = {'id': 'Visitor_visit', 'class': cls, 'method-name': lookup, 'focal-method': 'void visit() {}',
           'test-class': 'p.Visitor_visit_Test', 'test-path': 'src/test/java/p/Visitor_visit_Test.java'}

    def prepare(args):
        assert args == [str(ds)]
        assert read_json(ds/'dataset_meta.json')[0]['method_name_to_idx'] == {cls + '.' + lookup: 0}
        (ds/'dataset_info.json').write_text(json.dumps({'fixture': {'focal-methods': [row]}}))

    classes = {'extractor.SpoonExtractor': lambda *args: extractor(visit(1, 'p.Outer$X')),
               'DatasetPrepare': SimpleNamespace(main=prepare)}
    monkeypatch.setitem(sys.modules, 'jpype', SimpleNamespace(JClass=classes.__getitem__, shutdownJVM=lambda: None))
    monkeypatch.setattr(ktester_worker, 'start_jvm', lambda: None)
    ktester_worker.dataset()
    assert read_json(population/'dataset_info.raw.json')['fixture']['focal-methods'] == [row]
    assert read_json(population/'dataset_info.json')['fixture']['focal-methods'] == [dict(row, **{'method-name': original})]
    assert read_json(population/'dataset_meta.json')[0]['method_name_to_idx'] == {cls + '.' + original: 0}
    with (population/'ktester-population.csv').open() as stream:
        assert [r['method-name'] for r in csv.DictReader(stream)] == [original]
    with (population/'ktester-lookup-spellings.csv').open() as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == ['class', 'original', 'lookup']
        assert list(reader) == ([{'class': cls, 'original': original, 'lookup': lookup}] if original != lookup else [])


def test_population_preserves_overloads_after_author_id_collisions():
    signatures = ['visit(B.X)', 'visit(A.X)', 'visit(A.Y)', 'visit(B.Y)']
    ids = ['Visitor_visit', 'Visitor_visit_2', 'Visitor_visit_2', 'Visitor_visit_2']
    rows = [{'id': ident, 'class': 'p.Visitor', 'package': 'p', 'method-name': signature,
             'test-class': f'p.{ident}_Test', 'test-path': f'src/test/java/p/{ident}_Test.java'}
            for signature, ident in zip(signatures, ids)]
    with pytest.raises(ValueError, match=r'p\.Visitor\.visit\(A\.X\) \(2 entries\)'):
        renumber([rows[1], rows[1]])
    methods = ['p.Visitor.' + s for s in signatures]
    rows = renumber(rows)
    assert validate_population(rows, methods) == {'valid': True, 'entries': 4, 'unique_ids': 4}
    assert [r['method-name'] for r in rows] == signatures
    assert [r['id'] for r in rows] == ['Visitor_visit', 'Visitor_visit_2', 'Visitor_visit_3', 'Visitor_visit_4']

    rows[1]['test-class'] = 'another.package.Visitor_visit_Test'
    with pytest.raises(ValueError) as error:
        validate_population(rows, methods)
    assert all(s in str(error.value) for s in ('Visitor_visit_Test.java', methods[0], methods[1]))
