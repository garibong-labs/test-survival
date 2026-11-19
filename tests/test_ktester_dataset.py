from types import SimpleNamespace

import pytest

from test_survival.adapters.ktester_worker import match_class_methods, validate_population
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


def test_nested_overloads_keep_their_index_spelling_and_spoon_order():
    spoon = extractor(visit(12, 'p.A$X'), visit(10, 'p.B$X'), visit(15, 'p.C$X'))
    indexed = index('visit(A.X)', 'visit(C.X)', 'visit(B.X)')
    # C.X is in the index but was ineligible in the source inventory.
    eligible = ['visit(p.A.X)', 'visit(B.X)']
    assert match_class_methods('p.Visitor', eligible, spoon, indexed) == [
        'p.Visitor.visit(B.X)', 'p.Visitor.visit(A.X)']


def test_nested_array_overloads_keep_the_entire_enclosing_chain():
    spoon = extractor(visit(4, 'p.Host$A$Inner$X[]'), visit(8, 'p.Host$B$Inner$X[]'))
    eligible = ['visit(B.Inner.X[])', 'visit(A.Inner.X...)']
    indexed = index('visit(B.Inner.X[])', 'visit(A.Inner.X[])')
    assert match_class_methods('p.Host', eligible, spoon, indexed) == [
        'p.Host.visit(A.Inner.X[])', 'p.Host.visit(B.Inner.X[])']


def test_unambiguous_method_keeps_simple_name_matching():
    assert match_class_methods('p.Visitor', ['visit(X<String>...)'],
                               extractor(visit(1, 'p.Outer$X[]')), index('visit(X<String>[])')) == [
                                   'p.Visitor.visit(X<String>[])']


@pytest.mark.parametrize(('eligible', 'indexed'), [
    (['visit(X)'], index('visit(A.X)', 'visit(B.X)')),
    (['visit(A.X)'], index('visit(X)')),
    (['visit(A.X)', 'visit(B.X)'], index('visit(A.X)')),
])
def test_ambiguous_or_missing_match_identifies_class_and_overloads(eligible, indexed):
    with pytest.raises(ValueError) as error:
        match_class_methods('p.Visitor', eligible, extractor(visit(1, 'p.A$X'), visit(2, 'p.B$X')), indexed)
    assert all(s in str(error.value) for s in ('p.Visitor', 'visit(p.A.X)', 'visit(p.B.X)'))


def test_author_lookup_cannot_silently_select_a_different_focal_body():
    spoon = extractor(visit(1, 'p.BA$X'), visit(2, 'p.A$X'))
    signatures = ['visit(BA.X)', 'visit(A.X)']
    with pytest.raises(ValueError) as error:
        match_class_methods('p.Visitor', signatures, spoon, index(*signatures))
    assert all(s in str(error.value) for s in ['p.Visitor', *signatures])


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
