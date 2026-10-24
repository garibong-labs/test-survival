def test_cumulative_loss_carry_over_and_no_resurrection():
    from test_survival.rules.survival import advance
    rows=[{'id':'a','state':'fail'},{'id':'b','state':'pass'}]
    alive,lost=advance({'a','b'},rows)
    assert alive=={'b'} and [r['id'] for r in lost]==['a']
    assert advance(alive,[{'id':'a','state':'pass'},{'id':'b','state':'fail'}],evaluated=False)==({'b'},[])
    assert advance(alive,[{'id':'a','state':'pass'},{'id':'b','state':'pass'}])==({'b'},[])


def test_unchanged_module_checks_sources_and_pom():
    from test_survival.rules.survival import unchanged_module
    assert unchanged_module(['README.md','other/src/A.java'],'module')
    assert not unchanged_module(['module/pom.xml'],'module')
    assert not unchanged_module(['src/test/A.java'])
