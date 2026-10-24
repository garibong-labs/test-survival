from test_survival.rules.scope import select_scope
from test_survival.rules.dev_denominator import denominator
from test_survival.rules.matching import renumber, subsample


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
    assert [r['class'] for r in chosen]==['C','B']
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

