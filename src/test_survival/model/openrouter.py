"""stdlib OpenRouter client with resumable, credential-free call ledgers."""
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

from .credentials import _read
from ..io import now,read_json,write_json,atomic_text

ENDPOINT='https://openrouter.ai/api/v1/chat/completions'


class OpenRouter:
    def __init__(self,model):
        self.model=model

    def complete(self,prompt,params,directory):
        directory=Path(directory)
        body=dict(model=self.model['name'],temperature=params['temperature'],max_tokens=params['max_tokens'],messages=[dict(role='user',content=prompt)])
        if (directory/'run.json').exists():
            if read_json(directory/'request.json')!=body:raise RuntimeError('Existing call has different request parameters')
            return read_json(directory/'run.json'),(directory/'response.md').read_text()
        if (directory/'request.json').exists():
            raise RuntimeError('Interrupted model call has an unknown outcome; inspect ledger before retrying')
        key=_read(self.model)
        if not key:raise RuntimeError('KEY_MISSING')
        if key in json.dumps(body):raise RuntimeError('KEY_OK (value never logged); credential in request suppressed')
        directory.mkdir(parents=True,exist_ok=True)
        write_json(directory/'request.json',body)
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
        write_json(directory/'run.json',record)
        return record,text
