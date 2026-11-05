"""Durable per-context gzip-member journal, with legacy directory readers."""
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4
import zlib

SUCCESS_LOG_LIMIT = 16 * 1024


def log_payload(path, *, success=False):
    raw = Path(path).read_bytes() if Path(path).exists() else b''
    text = raw.decode('utf-8', errors='replace')
    encoded = text.encode('utf-8')
    truncated = success and len(encoded) > SUCCESS_LOG_LIMIT
    if truncated:
        text = encoded[:SUCCESS_LOG_LIMIT].decode('utf-8', errors='ignore')
    return dict(text=text, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), truncated=truncated)


def members(data):
    """Return complete members and the boundary before a damaged/incomplete tail."""
    offset = 0; decoded = []
    while offset < len(data):
        decoder = zlib.decompressobj(31)
        try:
            plain = decoder.decompress(data[offset:])
        except zlib.error:
            break
        if not decoder.eof:
            break
        used = len(data)-offset-len(decoder.unused_data)
        # Reject non-JSON members too; a recovered boundary is a record boundary.
        try:
            row = json.loads(plain)
        except (ValueError, UnicodeError):
            break
        if not isinstance(row, dict):break
        decoded.append(row);offset += used
    return decoded, offset


class Journal:
    def __init__(self, exp, context):
        self.exp = Path(exp)
        index = context.get('index')
        name = str(index) if index is not None else 'stage-'+str(context.get('phase') or context.get('stage') or context.get('action') or 'unknown')
        # Do not let stage names escape the ledger root or collide after sanitizing.
        if not re.fullmatch(r'[A-Za-z0-9_-]+',name):
            name = 'stage-'+hashlib.sha256(name.encode()).hexdigest()[:16]
        self.path = self.exp/'results/compile-events'/(name+'.jsonl.gz')
        self.lock = self.path.with_name(name+'.lock')

    def reference(self, event_id):
        return str(self.path.relative_to(self.exp))+'#'+event_id

    def append(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = gzip.compress((json.dumps(value,ensure_ascii=False,allow_nan=False)+'\n').encode(), compresslevel=6, mtime=0)
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            # The lock file also stores the last fsynced record boundary. Normal
            # appends inspect only the tail, avoiding a quadratic full-ledger scan.
            lock.seek(0)
            try:offset=int(lock.read())
            except ValueError:offset=0
            with self.path.open('a+b') as stream:
                stream.seek(0,os.SEEK_END);size=stream.tell()
                if offset < 0 or offset > size:offset=0
                stream.seek(offset);tail=stream.read()
                _,valid=members(tail)
                if valid != len(tail):
                    damaged=self.path.with_name(self.path.name+'.damaged-'+uuid4().hex)
                    with damaged.open('xb') as evidence:
                        evidence.write(tail[valid:]);evidence.flush();os.fsync(evidence.fileno())
                    stream.truncate(offset+valid)
                stream.seek(0,os.SEEK_END);stream.write(payload);stream.flush();os.fsync(stream.fileno())
                boundary=stream.tell()
            # Checkpoint the already-fsynced member boundary without replacing
            # the persistent lock inode.
            os.pwrite(lock.fileno(),str(boundary).encode(),0)
            os.ftruncate(lock.fileno(),len(str(boundary)))
            os.fsync(lock.fileno())


def read_events(root):
    """Read both layouts; incomplete gzip tail leaves earlier records readable."""
    root=Path(root);result=[]
    for path in sorted(root.glob('*.jsonl.gz')):
        rows,_=members(path.read_bytes());result.extend(rows)
    for directory in sorted(root.iterdir()) if root.exists() else []:
        if not directory.is_dir():continue
        for kind,name in [('start','started'),('finish','finished'),('interrupted','interrupted')]:
            path=directory/(name+'.json')
            if path.is_file():
                row=dict(json.loads(path.read_text()),type=kind,event_id=directory.name)
                if kind=='finish' and (directory/'javac.txt').is_file():row['javac']=log_payload(directory/'javac.txt')
                result.append(row)
        if (directory/'toolchain.json').is_file():
            result.append(dict(type='toolchain',event_id=directory.name,result=json.loads((directory/'toolchain.json').read_text()),
                               log=log_payload(directory/'toolchain.log')))
    return result


def read_event(exp, reference):
    """Resolve old event-directory or new ledger#event references."""
    path,separator,event_id=reference.partition('#')
    path=Path(exp)/path
    if separator:
        rows,_=members(path.read_bytes())
        return [r for r in rows if r.get('event_id')==event_id]
    return [r for r in read_events(path.parent) if r.get('event_id')==path.name]
