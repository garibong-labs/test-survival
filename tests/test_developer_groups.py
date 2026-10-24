import pytest
from test_survival.rules.dev_denominator import coverage_scoped,validate_derived_subsets,derive_record
from test_survival.rules.commits import skip_reason


def test_coverage_scoped_requires_passing_alone_and_scope_line():
    methods=[dict(**{'class':'C'},method=str(i),kind='plain',stable='yes') for i in range(3)]
    coverage=[dict(**{'class':'C'},method=str(i),scope_lines_covered=n,status=s) for i,n,s in [(0,3,'pass'),(1,0,'pass'),(2,4,'fail')]]
    kept,excluded=coverage_scoped(methods,coverage)
    assert [r['method'] for r in kept]==['0']
    assert [r['reason'] for r in excluded]==['no-scope-line-covered','failed-alone']
    validate_derived_subsets([{'name':'scope','derive_from':'dev'}],{'dev':methods,'scope':kept})
    with pytest.raises(ValueError,match='subset'):
        validate_derived_subsets([{'name':'dev','derive_from':'scope'}],{'dev':methods,'scope':kept})


@pytest.mark.parametrize('paths,module,mode,skip',[
    (['README.md'],'.','none',False),(['README.md'],'.','module',False),
    (['core2/src/main/A.java'],'core','module',True),(['core/README.md'],'core','module',False),
    (['core/src/test/T.java'],'core','main-sources',True),(['core/src/main/resources/a'],'core','main-sources',False),
    (['pom.xml'],'core','module',False),(['other/pom.xml'],'core','main-sources',False),
    (['src/main/A.java'],'.','main-sources',False),(['src/test/T.java'],'.','main-sources',True),
])
def test_skip_untouched_paths(paths,module,mode,skip):
    assert bool(skip_reason(paths,module,mode))==skip


def test_derived_raw_keeps_only_child_methods_and_does_not_mutate_source():
    methods=[{'id':str(i),'state':'pass'} for i in range(2)]
    raw={'index':1,'sha':'abc','group':'dev','phase':'A','status':'evaluated','methods':methods,'requested_ids':['0','1'],'files':[{'methods':methods,'runs':[{'command':['launcher']}]}]}
    child=derive_record(raw,'scope',[methods[1]])
    assert child['derived_from']=='dev' and child['requested_ids']==['1']
    assert child['methods']==[methods[1]] and child['files'][0]['methods']==[methods[1]]
    assert len(raw['methods'])==2 and len(raw['files'][0]['methods'])==2
