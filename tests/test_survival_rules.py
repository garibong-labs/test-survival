def test_cumulative_loss_carry_over_and_no_resurrection():
    from test_survival.rules.survival import advance
    rows=[{'id':'a','state':'fail'},{'id':'b','state':'pass'}]
    alive,lost=advance({'a','b'},rows)
    assert alive=={'b'} and [r['id'] for r in lost]==['a']
    assert advance(alive,[{'id':'a','state':'pass'},{'id':'b','state':'fail'}],evaluated=False)==({'b'},[])
    assert advance(alive,[{'id':'a','state':'pass'},{'id':'b','state':'pass'}])==({'b'},[])
