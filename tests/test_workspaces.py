import json
from pathlib import Path
import threading
import time

import pytest

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import validate
from test_survival.io import read_json, write_json
from test_survival.work import release_checkout, finish_checkouts, remove_owned


def adapter_fixture(tmp_path, monkeypatch):
    config=validate(dict(name='workspace', anchor='t', project={'repo':str(tmp_path)}))
    adapter=JavaMavenAdapter(tmp_path, config)
    calls=[]
    def checkout(index, sha):
        folder=adapter.folder(index);folder.mkdir(parents=True,exist_ok=True)
        (folder/'pom.xml').write_text('<project/>')
        write_json(tmp_path/f'work/checkouts/{index}.json',dict(sha=sha))
    def resolve(folder,index,**kw):
        path=tmp_path/'work/classpaths/cp.txt';path.parent.mkdir(parents=True,exist_ok=True);path.write_text('jar')
        return 'pom',path,[]
    def execute(command,folder,log,timeout):
        calls.append(command);log.parent.mkdir(parents=True,exist_ok=True);log.write_text('original log')
        (folder/'target/classes').mkdir(parents=True,exist_ok=True)
        return dict(command=command,exit_code=0,log=str(log.relative_to(tmp_path)))
    monkeypatch.setattr(adapter,'checkout',checkout)
    monkeypatch.setattr(adapter.cache,'resolve',resolve)
    monkeypatch.setattr(adapter.runner,'execute',execute)
    monkeypatch.setattr(adapter,'dependency_identity',lambda *a:[dict(path='jar',sha256='original')])
    return adapter,config,calls


def test_release_restore_preserves_original_build_and_logs(tmp_path,monkeypatch):
    adapter,config,calls=adapter_fixture(tmp_path,monkeypatch)
    first=adapter.build(2,'sha')
    originals={p:p.read_bytes() for p in (tmp_path/'results/builds').rglob('*') if p.is_file()}
    release_checkout(tmp_path,config,2)
    assert not adapter.folder(2).exists() and not (tmp_path/'work/checkouts/2.json').exists()
    assert adapter.build(2,'sha')==first
    assert originals=={p:p.read_bytes() for p in originals}
    assert len(calls)==2
    assert len(list((tmp_path/'results/workspace-restorations/2').glob('*/result.json')))==1
    assert read_json(tmp_path/'state/workspace-2.json')['status']=='completed'


@pytest.mark.parametrize('mismatch',['dependency','verdict','sha'])
def test_disagreement_stops_without_blessing_restored_classes(tmp_path,monkeypatch,mismatch):
    adapter,config,calls=adapter_fixture(tmp_path,monkeypatch)
    adapter.build(2,'sha');original=(tmp_path/'results/builds/2.json').read_bytes()
    release_checkout(tmp_path,config,2)
    if mismatch=='dependency':monkeypatch.setattr(adapter,'dependency_identity',lambda *a:[])
    if mismatch=='verdict':
        def fail(command,folder,log,timeout):
            log.parent.mkdir(parents=True,exist_ok=True);log.write_text('failed')
            (folder/'target/classes').mkdir(parents=True,exist_ok=True)
            return dict(exit_code=1)
        monkeypatch.setattr(adapter.runner,'execute',fail)
    for _ in range(2):
        with pytest.raises(RuntimeError,match='disagrees'):
            adapter.build(2,'other' if mismatch=='sha' else 'sha')
        assert (tmp_path/'results/builds/2.json').read_bytes()==original
    if mismatch!='sha':assert adapter.folder(2).exists()


def test_same_index_restoration_serialized(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    adapter,config,calls=adapter_fixture(tmp_path,monkeypatch)
    adapter.build(2,'sha');release_checkout(tmp_path,config,2)
    barrier=threading.Barrier(3)
    def restore():barrier.wait();return adapter.build(2,'sha')
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows=list(pool.map(lambda _:restore(),range(3)))
    assert len(calls)==2 and rows[0]==rows[1]==rows[2]


def test_failed_index_anchor_and_legacy_are_retained(tmp_path,monkeypatch):
    adapter,config,_=adapter_fixture(tmp_path,monkeypatch)
    for i in (0,2,3):adapter.build(i,'sha')
    write_json(tmp_path/'results/builds/3.json',dict(build='fail',sha='sha'))
    release_checkout(tmp_path,config,0);release_checkout(tmp_path,config,3)
    assert adapter.folder(0).exists() and adapter.folder(3).exists()
    finish_checkouts(tmp_path,config)
    assert not adapter.folder(0).exists() and not adapter.folder(2).exists()
    assert adapter.folder(3).exists()
    (tmp_path/'results/workspace-identities/3.json').unlink()
    release_checkout(tmp_path,config,3)
    assert adapter.folder(3).exists()


def test_parent_symlink_never_deletes_other_experiment(tmp_path):
    outside=tmp_path/'outside';outside.mkdir();(outside/'value').write_text('keep')
    exp=tmp_path/'exp';exp.mkdir();(exp/'work').symlink_to(outside,target_is_directory=True)
    with pytest.raises(OSError):remove_owned(exp,'work/value')
    assert (outside/'value').read_text()=='keep'
    with pytest.raises(ValueError):remove_owned(exp,'work/../outside')
