from copy import deepcopy
import pytest
from test_survival.adapters import java_worker
from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import validate
from test_survival.io import write_json
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


@pytest.mark.parametrize('message,expected_runs,expected_state', [
    ('java.net.BindException: Address already in use', ['run1','environment-1'], 'pass'),
    ('assertion failure', ['run1'], 'error'),
])
def test_single_method_coverage_retries_only_environment_failures(tmp_path,monkeypatch,message,expected_runs,expected_state):
    for name,value in {'EXP':tmp_path,'SHARED':tmp_path,'DEVELOPER':True,
                       'JVM':{'launcher':'1g'},'TIMEOUTS':{'test_class':120},'FLAKY_RERUN':3,
                       'ENVIRONMENT_ERRORS':{**SETTINGS,'backoff_seconds':0}}.items():
        monkeypatch.setattr(java_worker,name,value,raising=False)
    row={'id':'one','class':'Fixture','method':'one'}
    monkeypatch.setattr(java_worker,'classpath',lambda _: (tmp_path,''))
    monkeypatch.setattr(java_worker,'selectors',lambda *_: {'one':['Fixture#one']})
    calls=[]
    def execute(command,cwd,log,timeout):
        calls.append(log.parent.name)
        assert command.count('--select-method')==1
        assert any('destfile='+str(tmp_path/'jacoco.exec') in arg for arg in command)
        return {'timeout':False,'exit_code':0}
    monkeypatch.setattr(java_worker,'execute',execute)
    monkeypatch.setattr(java_worker,'parse_reports',lambda folder: {('Fixture','one'):[
        {'state':'error','message':message} if folder.name=='run1' else {'state':'pass','message':''}]})
    result=java_worker.run_file(0,'dev','Fixture.java',[row],tmp_path/'job',tmp_path/'jacoco.exec',
                               retry=False,environment_retry=True,precompiled=(tmp_path,{'exit_code':0}))
    method,=result['methods']
    assert calls==expected_runs
    assert method['state']==expected_state and not method['flaky']
    if expected_state=='pass':assert method['environment_reruns']==1


def test_method_coverage_cache_depends_on_environment_settings(tmp_path,monkeypatch):
    config=validate({'name':'fixture','project':{'repo':str(tmp_path)},'anchor':'t'})
    adapter=JavaMavenAdapter(tmp_path,config)
    (tmp_path/'Fixture.java').write_text('class Fixture {}')
    write_json(tmp_path/'results/anchor.json',{'sha':'anchor'})
    row={'class':'Fixture','method':'one','file':'Fixture.java'}
    calls=[]
    def worker(job):
        calls.append(job)
        assert job['retry'] is False and job['environment_retry'] is True
        result={k:job[k] for k in ['input_sha256','runner_sha256','sha']}
        write_json(tmp_path/job['output'],result)
        (tmp_path/job['coverage_xml']).touch()
        return result
    monkeypatch.setattr(adapter,'worker',worker)
    adapter.method_coverage(row)
    adapter.method_coverage(row)
    assert len(calls)==1
    config['survival']['environment_errors']['patterns']=['java.net.BindException']
    adapter.method_coverage(row)
    assert len(calls)==2
