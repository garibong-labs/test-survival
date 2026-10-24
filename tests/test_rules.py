from test_survival.rules.scope import select_scope
from test_survival.rules.dev_denominator import denominator
from test_survival.rules.matching import renumber, subsample
import pytest


def test_change_ranked_stops_before_exceeding_limit():
    rows=[{'class':'A','m':40,'c':10,'accessors':0},{'class':'B','m':45,'c':9,'accessors':0},{'class':'C','m':10,'c':8,'accessors':0}]
    c={'min_methods':3,'exclude_accessor_heavy':True,'rule':'change-ranked','target_methods':[50,80],'max_classes':10}
    chosen,eligible=select_scope(rows,c)
    assert [r['class'] for r in chosen]==['A']
    assert eligible==3


def test_accessor_threshold_ties_and_targeted_developer_matching():
    rows=[{'class':'B','m':4,'c':2,'accessors':2},{'class':'A','m':4,'c':2,'accessors':3},{'class':'C','m':5,'c':2,'accessors':0}]
    c={'min_methods':3,'exclude_accessor_heavy':True,'rule':'change-ranked','target_methods':[8,12],'max_classes':10}
    chosen,_=select_scope(rows,c)
    assert [r['class'] for r in chosen]==['B','C']
    methods=[{'class':c,'method':'test','kind':'plain','stable':'yes'} for c in ['p.JsonTest','p.JsonExtraTest','p.JsonNodeTest','q.JsonTest']]
    assert [r['class'] for r in denominator(methods,'scope-targeted',[{'class':'p.Json'}],{'Node'})]==['p.JsonTest','p.JsonExtraTest']


def test_renumber_preserves_unique_ids_and_is_idempotent():
    rows=[{'id':'A_m','class':'p.A','method-name':sig,'package':'p','test-class':'p.A_m_Test','test-path':'src/test/java/p/A_m_Test.java'} for sig in ['m(int, int)','m()']]
    rows.append({'id':'A_m_2','class':'p.A','method-name':'m(int)','package':'p','test-class':'p.A_m_2_Test','test-path':'src/test/java/p/A_m_2_Test.java'})
    output=renumber(rows)
    assert [r['id'] for r in output]==['A_m_3','A_m','A_m_2']
    assert output[-1]==rows[-1]
    assert renumber(output)==output
    assert [r['id'] for r in rows]==['A_m','A_m','A_m_2']


def test_subsample_is_fixed_and_preserves_input_order():
    rows=list(range(100))
    picked=subsample(rows,20,'ktester')
    assert len(picked)==20 and picked==sorted(picked)
    assert picked==subsample(rows,20,'ktester')
    assert picked!=subsample(rows,20,'ouedraogo')


@pytest.mark.parametrize('rule,rank_by,expected,eligible', [
    ('change-ranked','change-count',['p.B','p.C','p.A'],3),
    ('change-ranked','method-count',['p.C','p.A','p.B'],3),
    ('dedicated-test','change-count',['p.B','p.A'],2),
    ('dedicated-test','method-count',['p.A','p.B'],2),
])
def test_candidate_rule_and_ranking_are_independent(rule,rank_by,expected,eligible):
    rows=[dict(**{'class':'p.A'},m=4,c=1,has_test='yes'),dict(**{'class':'p.B'},m=3,c=10,has_test='yes'),dict(**{'class':'p.C'},m=8,c=5,has_test='no')]
    config=dict(rule=rule,rank_by=rank_by,min_methods=3,exclude_accessor_heavy=True,target_methods=[20,30],max_classes=10)
    chosen,count=select_scope(rows,config)
    assert [r['class'] for r in chosen]==expected
    assert count==eligible


def test_default_rank_is_change_count_for_dedicated_candidates():
    rows=[dict(**{'class':'p.A'},m=10,c=1,has_test='yes'),dict(**{'class':'p.B'},m=3,c=10,has_test='yes')]
    config=dict(rule='dedicated-test',min_methods=3,exclude_accessor_heavy=True,target_methods=[20,30],max_classes=10)
    assert [r['class'] for r in select_scope(rows,config)[0]]==['p.B','p.A']


def test_method_count_ties_use_only_fully_qualified_name():
    rows=[dict(**{'class':'p.B'},m=4,c=100,has_test='yes'),dict(**{'class':'p.A'},m=4,c=1,has_test='yes')]
    config=dict(rule='dedicated-test',rank_by='method-count',min_methods=3,exclude_accessor_heavy=True,target_methods=[8,12],max_classes=10)
    assert [r['class'] for r in select_scope(rows,config)[0]]==['p.A','p.B']


@pytest.mark.parametrize('rank_by',['change-count','method-count'])
def test_explicit_scope_preserves_supplied_order(rank_by):
    rows=[dict(**{'class':'p.A'},m=20,c=100),dict(**{'class':'p.B'},m=3,c=1)]
    config=dict(rule='explicit',rank_by=rank_by,classes=['p.B','p.A'],min_methods=3,exclude_accessor_heavy=True,target_methods=[50,80],max_classes=10)
    assert [r['class'] for r in select_scope(rows,config)[0]]==['p.B','p.A']

