import json
import pytest
from test_survival.model.openrouter import OpenRouter


class Response:
    status=200
    def __init__(self,value):self.value=value
    def read(self):return json.dumps(self.value).encode()
    def __enter__(self):return self
    def __exit__(self,*args):pass


def test_private_credential_and_resumable_call_ledger(tmp_path,monkeypatch):
    key='fake-credential-for-unit-test'
    monkeypatch.setenv('SURVIVAL_TEST_KEY',key)
    model={'name':'test/model','env_key':'SURVIVAL_TEST_KEY','env_file':str(tmp_path/'missing')}
    calls=[]
    def request(req,timeout):
        calls.append(req)
        assert req.headers['Authorization']=='Bearer '+key
        return Response({'id':'request-1','model':'test/model','choices':[{'message':{'content':'test content'},'finish_reason':'stop'}],'usage':{'completion_tokens':2}})
    monkeypatch.setattr('urllib.request.urlopen',request)
    client=OpenRouter(model);params={'temperature':0.7,'max_tokens':10}
    record,text=client.complete('prompt',params,tmp_path/'call')
    assert record['status']=='ok' and text=='test content'
    assert client.complete('prompt',params,tmp_path/'call')==(record,text)
    assert len(calls)==1
    assert all(key not in p.read_text() for p in (tmp_path/'call').iterdir())
    with pytest.raises(RuntimeError,match='different request'):
        client.complete('different',params,tmp_path/'call')


def test_interrupted_and_credential_echo_are_not_logged(tmp_path,monkeypatch):
    key='fake-private-value'
    monkeypatch.setenv('SURVIVAL_TEST_KEY',key)
    client=OpenRouter({'name':'test/model','env_key':'SURVIVAL_TEST_KEY','env_file':str(tmp_path/'missing')})
    monkeypatch.setattr('urllib.request.urlopen',lambda *a,**k:Response({'choices':[{'message':{'content':key}}]}))
    record,text=client.complete('prompt',{'temperature':0,'max_tokens':10},tmp_path/'echo')
    assert record['status']=='credential-echo' and text==''
    assert all(key not in p.read_text() for p in (tmp_path/'echo').iterdir())
    p=tmp_path/'interrupted';p.mkdir();(p/'request.json').write_text('{}')
    with pytest.raises(RuntimeError,match='unknown outcome'):
        client.complete('prompt',{'temperature':0,'max_tokens':10},p)


@pytest.mark.parametrize('env_file', ['absent', None])
def test_credentials_without_an_env_file_use_only_process_environment(monkeypatch, env_file):
    from test_survival.model.credentials import _read, key_available
    config = {'env_key': 'SURVIVAL_TEST_KEY'}
    if env_file != 'absent':
        config['env_file'] = env_file
    monkeypatch.delenv('SURVIVAL_TEST_KEY', raising=False)
    assert _read(config) is None
    assert not key_available(config)
    monkeypatch.setenv('SURVIVAL_TEST_KEY', 'test-only-key')
    assert _read(config) == 'test-only-key'
    assert key_available(config)


def test_explicit_env_file_and_process_override(tmp_path, monkeypatch):
    from test_survival.model.credentials import _read
    path = tmp_path/'model.env'
    path.write_text('SURVIVAL_TEST_KEY="file-key"\n')
    config = {'env_key': 'SURVIVAL_TEST_KEY', 'env_file': str(path)}
    monkeypatch.delenv('SURVIVAL_TEST_KEY', raising=False)
    assert _read(config) == 'file-key'
    monkeypatch.setenv('SURVIVAL_TEST_KEY', 'process-key')
    assert _read(config) == 'process-key'
