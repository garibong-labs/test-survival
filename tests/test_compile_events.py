import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from test_survival.adapters.compile_events import Journal, log_payload, read_event, read_events, SUCCESS_LOG_LIMIT


def test_parallel_process_appends_one_context(tmp_path):
    script='''from pathlib import Path
import sys
from test_survival.adapters.compile_events import Journal
j=Journal(Path(sys.argv[1]),{'index':7})
for i in range(25):
 j.append({'type':'start','event_id':sys.argv[2]+'-'+str(i)})
 j.append({'type':'finish','event_id':sys.argv[2]+'-'+str(i)})
'''
    children=[subprocess.Popen([sys.executable,'-c',script,str(tmp_path),str(n)]) for n in range(4)]
    assert [p.wait(timeout=20) for p in children]==[0]*4
    rows=read_events(tmp_path/'results/compile-events')
    assert len(rows)==200 and len({r['event_id'] for r in rows})==100
    for ident in {r['event_id'] for r in rows}:
        assert [r['type'] for r in rows if r['event_id']==ident]==['start','finish']
    journal=Journal(tmp_path,{'index':7})
    assert int(journal.lock.read_text())==journal.path.stat().st_size
    assert len(list(journal.path.parent.iterdir()))==2


def test_crashed_gzip_tail_preserved_before_resume(tmp_path):
    journal=Journal(tmp_path,{'index':0})
    journal.append(dict(type='start',event_id='one'))
    inode=journal.lock.stat().st_ino
    broken=gzip.compress(b'{"type":"finish"}\n')[:-7]
    with journal.path.open('ab') as stream:stream.write(broken)
    assert read_event(tmp_path,journal.reference('one'))==[dict(type='start',event_id='one')]
    journal.append(dict(type='start',event_id='two'))
    assert [r['event_id'] for r in read_events(journal.path.parent)]==['one','two']
    assert next(journal.path.parent.glob('*.damaged-*')).read_bytes()==broken
    assert journal.lock.stat().st_ino==inode


def test_complete_member_before_checkpoint_crash_is_not_lost(tmp_path):
    journal=Journal(tmp_path,{'phase':'screen'})
    journal.append(dict(type='start',event_id='one'))
    with journal.path.open('ab') as stream:stream.write(gzip.compress(b'{"type":"finish","event_id":"one"}\n'))
    journal.append(dict(type='start',event_id='two'))
    assert len(read_events(journal.path.parent))==3
    # A torn checkpoint also recovers from the gzip members, not a guessed offset.
    journal.lock.write_text('torn')
    journal.append(dict(type='finish',event_id='two'))
    assert len(read_events(journal.path.parent))==4


def test_success_truncation_and_full_failure_unicode(tmp_path):
    raw=('before\n'+'한'*20000+'\nafter').encode()
    path=tmp_path/'javac.txt';path.write_bytes(raw)
    small=log_payload(path,success=True);full=log_payload(path)
    assert small['truncated'] and len(small['text'].encode())<=SUCCESS_LOG_LIMIT
    assert full['text'].encode()==raw and not full['truncated']
    assert small['bytes']==len(raw) and small['sha256']==hashlib.sha256(raw).hexdigest()


def test_legacy_and_new_events_are_readable_together(tmp_path):
    old=tmp_path/'results/compile-events/old';old.mkdir(parents=True)
    (old/'started.json').write_text(json.dumps(dict(context={'index':0})))
    (old/'finished.json').write_text(json.dumps(dict(executed=True,result={'exit_code':1})))
    (old/'javac.txt').write_text('all old diagnostics')
    new=Journal(tmp_path,{'index':1});new.append(dict(type='start',event_id='new'))
    assert len(read_events(old.parent))==3
    records=read_event(tmp_path,'results/compile-events/old')
    assert records[1]['javac']['text']=='all old diagnostics'
    assert read_event(tmp_path,new.reference('new'))[0]['event_id']=='new'
