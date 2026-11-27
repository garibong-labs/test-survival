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


@pytest.mark.parametrize('nested', [False, True])
def test_resend_archives_only_interrupted_attempts_and_keeps_index(tmp_path, monkeypatch, nested):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timedelta
    from hashlib import sha256
    from test_survival.io import read_json, write_json
    from test_survival.pipeline import run_session

    model = dict(name='fixture', env_key='TEST_MODEL_KEY')
    params = dict(temperature=0, max_tokens=10)
    monkeypatch.setenv('TEST_MODEL_KEY', 'offline-fake-key')
    requests = []
    def http(request, timeout):
        requests.append(json.loads(request.data)['messages'][0]['content'])
        return Response({'choices': [{'message': {'content': 'again'}}]})
    monkeypatch.setattr('urllib.request.urlopen', http)
    workspace = tmp_path/'results/policy/pools/native/1' if nested else tmp_path
    with run_session(tmp_path, resend_interrupted=True) as run:
        if nested:
            write_json(workspace/'state/run-owner.json', dict(experiment=str(tmp_path), run_id=run['run_id']))
        client = OpenRouter(model, exp=workspace)
        runs = workspace/'generated/native/round1/runs'
        completed = runs/'completed'
        client.complete('saved', params, completed)
        parent = runs/'retried'
        write_json(parent/'request.json', client.request('retry', params))
        write_json(parent/'run.json', dict(status='http-error'))
        (parent/'response.md').write_text('failed')
        preserved = {p:p.read_bytes() for folder in (completed, parent) for p in folder.iterdir()}
        attempts = {runs/'interrupted':'first', parent/'attempt-2':'retry'}
        for path, prompt in attempts.items():
            write_json(path/'request.json', client.request(prompt, params))
            (path/'partial').write_text(prompt)
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(client.complete, prompt, params, path) for path, prompt in attempts.items()]
            for future in futures:
                assert future.result()[1] == 'again'
        client.complete('saved', params, completed)
        assert sorted(requests) == ['first', 'retry', 'saved']
        assert preserved == {p:p.read_bytes() for p in preserved}
        history = read_json(tmp_path/'interrupted-ledgers/index.json')
        assert len(history) == 2
        assert {tmp_path/r['original_path'] for r in history} == set(attempts)
        for entry in history:
            path = tmp_path/entry['original_path']
            archive = tmp_path/entry['new_path']
            assert archive.relative_to(tmp_path).parts[0] == 'interrupted-ledgers'
            assert archive.relative_to(tmp_path).parts[2:] == path.relative_to(tmp_path).parts
            assert datetime.fromisoformat(entry['time']).utcoffset() == timedelta(0)
            assert entry['request_sha256'] == sha256((archive/'request.json').read_bytes()).hexdigest()
            assert (archive/'partial').read_text() == attempts[path]
            assert not (archive/'run.json').exists() and not (path/'partial').exists()
            assert read_json(path/'run.json')['status'] == 'ok'
    with run_session(tmp_path):
        client = OpenRouter(model, exp=tmp_path)
        write_json(tmp_path/'other/request.json', client.request('other', params))
        with pytest.raises(RuntimeError, match='unknown outcome.*--resend-interrupted'):
            client.complete('other', params, tmp_path/'other')
        assert len(requests) == 3


def test_index_write_failure_is_recovered_before_http(tmp_path, monkeypatch):
    from test_survival.io import read_json, write_json
    from test_survival.model import calls
    from test_survival.pipeline import run_session

    model = dict(name='fixture', env_key='TEST_MODEL_KEY')
    params = dict(temperature=0, max_tokens=10)
    ledger = tmp_path/'generated/call'
    monkeypatch.setenv('TEST_MODEL_KEY', 'offline-fake-key')
    write_json(ledger/'request.json', OpenRouter(model).request('same prompt', params))
    index = tmp_path/'interrupted-ledgers/index.json'
    requests = []
    def http(request, timeout):
        assert len(read_json(index)) == 1
        requests.append(request)
        return Response({'choices': [{'message': {'content': 'done'}}]})
    monkeypatch.setattr('urllib.request.urlopen', http)
    def fail_index(path, value):
        if path == index:
            raise OSError('index unavailable')
        write_json(path, value)
    with run_session(tmp_path, resend_interrupted=True), monkeypatch.context() as patch:
        patch.setattr(calls, 'write_json', fail_index)
        with pytest.raises(OSError, match='index unavailable'):
            OpenRouter(model, exp=tmp_path).complete('same prompt', params, ledger)
    assert not requests and not ledger.exists()
    with run_session(tmp_path):
        with pytest.raises(RuntimeError, match='unknown outcome.*--resend-interrupted'):
            OpenRouter(model, exp=tmp_path).complete('same prompt', params, ledger)
    assert not requests and (ledger/'request.json').exists()
    with run_session(tmp_path, resend_interrupted=True):
        OpenRouter(model, exp=tmp_path).complete('same prompt', params, ledger)
    entry, = read_json(index)
    assert (tmp_path/entry['new_path']/'request.json').is_file()
    assert not (tmp_path/'state/interrupted-ledger-move.json').exists()
    assert len(requests) == 1


def test_move_recovery_requires_consistent_experiment_paths(tmp_path):
    from test_survival.model.calls import recover_archive
    from test_survival.io import now, write_json
    write_json(tmp_path/'state/interrupted-ledger-move.json', dict(
        original_path='generated/round1/call', new_path='generated/round2/call', time=now()))
    saved = tmp_path/'generated/round2/call/run.json'
    write_json(saved, {'status': 'ok'})
    with pytest.raises(RuntimeError, match='inconsistent archive path'):
        recover_archive(tmp_path)
    assert saved.is_file()
