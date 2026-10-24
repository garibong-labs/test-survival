import importlib.util
from pathlib import Path

import pytest

from test_survival.io import write_csv, write_json

spec=importlib.util.spec_from_file_location('acceptance_comparison',Path(__file__).parents[1]/'scripts/compare_full_replay.py')
comparison=importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def test_failure_classification_pairs_both_rows_using_earlier_bind_exception(tmp_path):
    replay,reference=tmp_path/'replay',tmp_path/'reference'
    base=dict(index='5',sha='abc',group='dev',id='f|C|m',**{'class':'C'},method='m',kind='error',flaky='False')
    for folder,row,message in [(replay,{**base,'index':'6'},'Assertion failed: expected 4'),(reference,base,'java.net.BindException: Address already in use')]:
        write_csv(folder/'results/first-failures.csv',[row],list(base))
        write_json(folder/f"results/raw/{row['index']}-dev.json",{'methods':[{'id':row['id'],'message':message,'retry_message':message}]})
    rows=comparison.failure_differences(replay,reference)
    assert [(r['side'],r['classification']) for r in rows]==[('reference','port-collision'),('replay','port-collision')]
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


@pytest.mark.parametrize('first,second,first_index,second_index,group,accepted', [
    ('java.net.BindException', 'compile failure', 5, 5, 'dev', True),
    ('assertion failed', 'java.net.BindException', 6, 5, 'dev', True),
    ('assertion failed', 'java.net.BindException', 5, 6, 'dev', False),
    ('java.net.BindException', 'java.net.BindException', 5, 5, 'dev', False),
    ('java.net.ConnectException', 'compile failure', 5, 6, 'dev', False),
    ('Address already in use', 'compile failure', 5, 6, 'dev', False),
    ('java.net.BindException', 'compile failure', 5, 6, 'ktester', False),
])
def test_pairing_rule_requires_own_bind_message_and_correct_chronology(tmp_path, first, second, first_index, second_index, group, accepted):
    replay,reference=tmp_path/'replay',tmp_path/'reference'
    for root,index,message,kind in [(replay,first_index,first,'error'),(reference,second_index,second,'compile-fail')]:
        row=dict(index=str(index),group=group,id='m',kind=kind)
        write_csv(root/'results/first-failures.csv',[row],list(row))
        write_json(root/f'results/raw/{index}-{group}.json',{'methods':[{'id':'m','message':message}]})
    rows=comparison.failure_differences(replay,reference)
    assert len(rows)==2
    assert all((r['classification']=='port-collision')==accepted for r in rows)


def test_missing_counterpart_cannot_be_accepted_and_other_method_message_is_not_used(tmp_path):
    replay,reference=tmp_path/'replay',tmp_path/'reference'
    row=dict(index='5',group='dev',id='m',kind='error')
    write_csv(replay/'results/first-failures.csv',[row],list(row))
    write_csv(reference/'results/first-failures.csv',[],list(row))
    write_json(replay/'results/raw/5-dev.json',{'methods':[{'id':'other','message':'java.net.BindException'}]})
    item,=comparison.failure_differences(replay,reference)
    assert item['classification']=='unclassified' and not item['evidence']
    assert comparison.lost_method_audit(replay,reference)['dev']['replay_only']==['m']


def test_llm_projection_excludes_dev_only_events_but_detects_llm_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(comparison, 'FILES', ['table1-survival-by-event.md', 'survival.csv'])
    a,b=tmp_path/'a',tmp_path/'b'
    rows=[dict(index=str(i),dev_alive=str(3-i),dev_lost='1',ktester_alive='2',ktester_lost='0') for i in range(3)]
    header='# Table\n\n| Index | Subject | Dev | KTester |\n|---|---|---|---|\n'
    for root in [a,b]:
        write_csv(root/'results/survival.csv',rows,list(rows[0]))
        (root/'results/table1-survival-by-event.md').write_text(header+'| 0 | a \\| b | 3 | 2 |\n| 2 | end | 1 | 2 |\n')
    (a/'results/table1-survival-by-event.md').write_text(header+'| 0 | a \\| b | 3 | 2 |\n| 1 | Dev-only event | 2 | 2 |\n| 2 | end | 1 | 2 |\n')
    assert all(r['status']=='identical' for r in comparison.llm_file_comparisons(a,b))
    rows[1]['ktester_alive']='1'
    write_csv(b/'results/survival.csv',rows,list(rows[0]))
    assert comparison.llm_file_comparisons(a,b)[1]['status']=='differs'
    path=b/'results/table1-survival-by-event.md'
    path.write_text(path.read_text().replace('| 1 | 2 |','| 1 | 0 |'))
    assert comparison.llm_file_comparisons(a,b)[0]['status']=='differs'
