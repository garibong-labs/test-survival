"""Shared evidence reads scale with records, not methods; caches never span calls."""
from collections import Counter
from types import SimpleNamespace

import pytest

from test_survival.adapters.dependencies import RECORD_VERSION
from test_survival.io import read_json, write_json
from test_survival.policy import affected
from test_survival.policy.segments import key, segment
from test_survival.selection import carry, executed


def evidence():
    context = dict(known=True, entries=[])
    # Identical class/method names across two snapshots must stay distinct.
    tests = [dict(id=f'{digest}-{n}', file_hash=digest, execution_hash=digest,
                  **{'class':'Test'}, method=f'test{n}')
             for digest in ('first','second') for n in range(500)]
    rows = [executed(dict(t, state='pass', lines=[['src/main/java/A.java',n] for n in range(1,101)],
                         dependency_record=dict(version=RECORD_VERSION,selection='affected',files=['src/main/java/A.java'],unbounded=False)),
                     1,'one',context) for t in tests]
    return tests, rows, context


def count_reads(monkeypatch):
    reads = Counter(); keys = Counter()
    read, identify = affected.read_json, affected.key
    def counted(path, *args, **kwargs):
        reads[path] += 1
        return read(path, *args, **kwargs)
    def keyed(row):
        keys['calls'] += 1
        return identify(row)
    monkeypatch.setattr(affected,'read_json',counted)
    monkeypatch.setattr(affected,'key',keyed)
    return reads, keys


def test_resume_indexes_shared_origins_once_per_audit(tmp_path, monkeypatch):
    tests,rows,_ = evidence()
    b = SimpleNamespace(exp=tmp_path,options={'set':[{'name':'p'}]},
                        by_index={1:{'sha':'one'},2:{'sha':'two'}},evaluable=lambda _:True)
    write_json(tmp_path/'results/policy/p/segments.json', {'g':[segment(2,1,tests,[])]})
    origin = affected.logical_path(b,'p','g',2,1)
    carried = affected.logical_path(b,'p','g',2,2)
    write_json(origin,dict(sha='one',methods=rows),compress=True)
    write_json(carried,dict(sha='two',methods=[carry(r) for r in rows]),compress=True)
    physical = []
    for digest in ('first','second'):
        path=tmp_path/f'results/policy/runs/{digest}/1.json';physical.append(path)
        write_json(path,dict(sha='one',methods=[r for r in rows if r['execution_hash']==digest]),compress=True)
    before = {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    reads, keys = count_reads(monkeypatch)
    assert affected.records_complete(b)
    assert reads[origin] == reads[carried] == 1
    assert all(reads[p] == 1 for p in physical)
    assert keys['calls'] <= 5*len(tests)
    assert before == {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    # A new audit must notice changed evidence, even with colliding method names.
    changed=read_json(physical[1]);changed['methods'][-1]['lines']=[]
    write_json(physical[1],changed,compress=True)
    assert not affected.records_complete(b)


@pytest.mark.parametrize('source',['baseline','physical'])
def test_physical_indexes_shared_records_once_per_measurement(tmp_path, monkeypatch, source):
    tests,rows,context=evidence(); baseline_reads=Counter()
    baseline=tmp_path/'baseline.json'
    write_json(baseline,dict(sha='one',methods=rows),compress=True)
    def raw(index,group):
        baseline_reads[group]+=1
        return read_json(baseline) if source=='baseline' else {}
    b=SimpleNamespace(exp=tmp_path,by_index={1:{'sha':'one'}},groups=['g'],raw=raw,
                      scope={'classes':[{'file':'src/main/java/A.java','class':'A'}]})
    calls=[]
    def worker(job):
        calls.append(job)
        return dict(methods=[dict(t,state='pass',lines=[],dependency_record=rows[0]['dependency_record']) for t in job['methods']])
    adapter=SimpleNamespace(config={'project':{'repo':str(tmp_path),'module':'.'}},worker=worker)
    runs=SimpleNamespace(adapter=adapter,initialize=lambda:None,excluded_for=lambda tests:set(),needed=set(),
                         path=lambda index,digest:tmp_path/f'results/policy/runs/{digest}/{index}.json')
    paths=[runs.path(1,digest) for digest in ('first','second')]
    if source=='physical':
        for path,digest in zip(paths,('first','second')):
            write_json(path,dict(sha='one',methods=[r for r in rows if r['execution_hash']==digest]))
    reads,keys=count_reads(monkeypatch)
    decisions=affected.Decisions(b,runs)
    observed=decisions.physical(1,{key(t):t for t in tests},context)
    assert observed == {key(r):r for r in rows}
    assert not calls
    assert baseline_reads == ({'g':1} if source=='baseline' else {})
    assert all(reads[p] == 1 for p in paths)
    assert keys['calls'] <= 4*len(tests)
    # Same coordinator, new call: replaced disk evidence must not stay cached.
    changed=read_json(paths[1]);changed['methods'][-1].pop('lines')
    write_json(paths[1],changed)
    write_json(baseline,dict(sha='one',methods=[]),compress=True)
    decisions.physical(1,{key(t):t for t in tests},context)
    assert len(calls)==1 and len(calls[0]['methods'])==1
    assert key(calls[0]['methods'][0])==key(tests[-1])
