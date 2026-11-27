"""stdlib OpenRouter client with resumable, credential-free call ledgers."""
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

from . import calls
from ..lifecycle import owner_experiment
from .credentials import _read
from ..io import now,read_json,write_json,atomic_text

ENDPOINT='https://openrouter.ai/api/v1/chat/completions'


class InterruptedModelCall(RuntimeError):
    """A request was published but its provider outcome is unknown."""


class OpenRouter:
    def __init__(self, model, *, exp=None):
        self.model = model
        self.exp = owner_experiment(exp) if exp is not None else None
        run = read_json(self.exp/'state/run.json', {}) if self.exp else {}
        self.run_id = run.get('run_id') if run.get('status') == 'running' else None
        self.resend_interrupted = bool(self.run_id and run.get('resend_interrupted'))

    def request(self,prompt,params):
        return dict(model=self.model['name'],temperature=params['temperature'],max_tokens=params['max_tokens'],messages=[dict(role='user',content=prompt)])

    def verify(self,prompt,params,directory):
        """Reject a recorded call whose request ledger does not match this request."""
        recorded=read_json(Path(directory)/'request.json')
        if recorded is None:raise RuntimeError('Recorded call has no request ledger: '+str(directory))
        if recorded!=self.request(prompt,params):raise RuntimeError('Existing call has different request parameters')

    def complete(self, prompt, params, directory):
        directory = Path(directory)
        body = self.request(prompt, params)
        with calls.admission_lock(self.exp):
            if self.exp:
                calls.recover_archive(self.exp)
            if (directory/'run.json').exists():
                self.verify(prompt, params, directory)
                return read_json(directory/'run.json'), (directory/'response.md').read_text()
            if self.exp and (self.exp/'state/stopping.json').exists():
                raise RuntimeError('Experiment stopped by user before starting model call')
            interrupted = (directory/'request.json').exists()
            if interrupted:
                if not self.resend_interrupted:
                    raise InterruptedModelCall(
                        f'Interrupted model call has an unknown outcome; inspect ledger before retrying: {directory}. '
                        '--resend-interrupted will move the ledger aside and send the request again')
                self.verify(prompt, params, directory)
            key = _read(self.model)
            if not key:
                raise RuntimeError('KEY_MISSING')
            if key in json.dumps(body):
                raise RuntimeError('KEY_OK (value never logged); credential in request suppressed')
            marker = calls.register(self.exp, directory, self.run_id) if self.run_id else None
            try:
                if interrupted:
                    calls.archive_interrupted(self.exp, directory)
                write_json(directory/'request.json', body)
            except BaseException:
                if marker:
                    marker.unlink()
                raise
        record, text = self._send(directory, body, prompt, key)
        with calls.admission_lock(self.exp):
            write_json(directory/'run.json', record)
            if marker:
                marker.unlink()
        return record, text

    def _send(self, directory, body, prompt, key):
        started=time.monotonic();timestamp=now();status='transport-error';http_status=None;data={};error_type=None
        try:
            request=urllib.request.Request(ENDPOINT,data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
            try:
                with urllib.request.urlopen(request,timeout=600) as response:
                    raw=response.read();http_status=response.status
            except urllib.error.HTTPError as exc:
                raw=exc.read();http_status=exc.code
            if key.encode() in raw:
                raw=b'{"error":"KEY_OK (value never logged); credential echo suppressed"}'
                status='credential-echo'
            else:
                status='ok' if http_status==200 else 'http-error'
            atomic_text(directory/'raw-response.json',raw.decode('utf-8',errors='replace'))
            try:data=json.loads(raw)
            except ValueError:status='invalid-json'
        except Exception as exc:
            error_type=type(exc).__name__
            write_json(directory/'raw-response.json',{'transport_error':error_type})
        choices=data.get('choices',[]) if isinstance(data,dict) else []
        choice=choices[0] if choices else {};content=choice.get('message',{}).get('content');text=content if isinstance(content,str) else ''
        if status=='ok' and not text:status='empty-content'
        atomic_text(directory/'response.md',text)
        usage=data.get('usage',{}) if isinstance(data,dict) else {};write_json(directory/'usage.json',usage)
        record=dict(timestamp=timestamp,latency=round(time.monotonic()-started,3),status=status,http_status=http_status,finish_reason=choice.get('finish_reason'),provider=data.get('provider') if isinstance(data,dict) else None,model_requested=self.model['name'],model_reported=data.get('model') if isinstance(data,dict) else None,request_id=data.get('id') if isinstance(data,dict) else None,usage=usage,error_type=error_type,prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),response_sha256=hashlib.sha256(text.encode()).hexdigest())
        return record,text
