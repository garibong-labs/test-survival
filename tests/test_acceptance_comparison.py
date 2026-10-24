import importlib.util
from pathlib import Path

from test_survival.io import write_csv, write_json

spec=importlib.util.spec_from_file_location('acceptance_comparison',Path(__file__).parents[1]/'scripts/compare_full_replay.py')
comparison=importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def test_failure_classification_uses_each_sides_actual_messages(tmp_path):
    replay,reference=tmp_path/'replay',tmp_path/'reference'
    base=dict(index='5',sha='abc',group='dev',id='f|C|m',**{'class':'C'},method='m',kind='error',flaky='False')
    for folder,row,message in [(replay,{**base,'index':'6'},'Assertion failed: expected 4'),(reference,base,'java.net.BindException: Address already in use')]:
        write_csv(folder/'results/first-failures.csv',[row],list(base))
        write_json(folder/f"results/raw/{row['index']}-dev.json",{'methods':[{'id':row['id'],'message':message,'retry_message':message}]})
    rows=comparison.failure_differences(replay,reference)
    assert [(r['side'],r['classification']) for r in rows]==[('reference','port-collision'),('replay','unclassified')]
    assert all(r['csv_line']==2 for r in rows)


def test_connect_error_rule_does_not_match_arbitrary_api_diagnostics():
    for message in ['java.net.ConnectException: Connection refused','Unable to bind to local address','EADDRINUSE']:
        assert comparison.PORT.search(message)
    for message in ['cannot find symbol: method connect()', 'expected connected status but was false','Read timed out']:
        assert not comparison.PORT.search(message)


def test_first_failure_comparison_ignores_row_order(tmp_path):
    a,b=tmp_path/'a',tmp_path/'b'
    rows=[{'index':'1','id':'a'},{'index':'2','id':'b'}]
    write_csv(a/'results/first-failures.csv',rows,['index','id'])
    write_csv(b/'results/first-failures.csv',rows[::-1],['index','id'])
    assert comparison.compare_file(a,b,'first-failures.csv')['status']=='identical'
    write_csv(b/'results/first-failures.csv',[rows[1],{'index':'3','id':'c'}],['index','id'])
    result=comparison.compare_file(a,b,'first-failures.csv')
    assert (result['replay_line'],result['reference_line'])==(2,3)
    assert result['replay']=='1,a' and result['reference']=='3,c'


def test_order_audit_distinguishes_initial_order_from_retries(tmp_path):
    a,b=tmp_path/'a',tmp_path/'b'
    launch={'command':['java','--select-method','C#m()']}
    for root,runs in [(a,[launch]),(b,[launch,launch])]:
        write_json(root/'results/raw/1-dev.json',{'index':1,'group':'dev','requested_ids':['m'],'methods':[{'id':'m'}],'files':[{'file':'C.java','runs':runs}]})
    result=comparison.order_audit(a,b)
    assert result['initial_per_commit_orders_equal']
    assert not result['all_per_commit_orders_equal']
    assert not result['differences'][0]['all_launch_order_including_retries']
