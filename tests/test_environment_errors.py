from copy import deepcopy
from test_survival.adapters.java_worker import rerun_environment_failures
from test_survival.rules.environment import matches

SETTINGS={'patterns':['java.net.BindException'],'max_reruns':3,'backoff_seconds':5}


def test_environment_retry_runs_class_and_marks_recovery_without_flaky():
    records=[dict(id='a',state='error',message='java.net.BindException',flaky=False),dict(id='b',state='fail',message='assertion',flaky=False)]
    waits=[];calls=[]
    def launch(attempt):
        calls.append(attempt)
        return {'a':('pass','') if attempt==2 else ('error','java.net.BindException'),'b':('pass','')}
    rerun_environment_failures(records,launch,SETTINGS,waits.append)
    assert calls==[1,2] and waits==[5,5]
    assert records[0]['state']=='pass' and records[0]['environment_reruns']==2 and not records[0]['flaky']
    assert records[1]['state']=='fail' and 'environment_reruns' not in records[1]


def test_exhaustion_keeps_last_failure_and_empty_patterns_leave_records_untouched():
    records=[dict(id='a',state='error',message='java.net.BindException',flaky=False)]
    before=deepcopy(records)
    rerun_environment_failures(records,lambda _: (_ for _ in ()).throw(AssertionError()),{**SETTINGS,'patterns':[]})
    assert records==before
    rerun_environment_failures(records,lambda n:{'a':('fail','final assertion '+str(n))},SETTINGS,lambda _:None)
    assert records[0]['state']=='fail' and records[0]['message']=='final assertion 3'
    assert records[0]['environment_reruns']==3 and not records[0]['flaky']
    assert matches('java.net.BindException: bind',SETTINGS['patterns'])
    assert not matches('bindexception',SETTINGS['patterns'])
