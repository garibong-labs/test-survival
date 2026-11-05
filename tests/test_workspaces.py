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


@pytest.mark.parametrize('fail_slow',[False,True])
@pytest.mark.parametrize('needs_coverage',[False,True])
def test_survival_waits_for_every_index_consumer_before_release(tmp_path,monkeypatch,fail_slow,needs_coverage):
    from contextlib import nullcontext
    from test_survival.stages import survive
    from test_survival.io import write_csv
    groups=['fast','slow']
    config=validate(dict(name='consumers',anchor='t',project={'repo':str(tmp_path)},
                         coverage={'at_anchor':False,'changed_line':needs_coverage},
                         dev={'groups':[dict(name=g,denominator='project') for g in groups]},
                         runner={'workers':{'survive':2}}))
    method=dict(id='T#test',file='T.java',execution_hash='source',**{'class':'T'},method='test',kind='plain')
    write_json(tmp_path/'results/frozen-denominators.json',{g:[method] for g in groups})
    write_json(tmp_path/'results/anchor.json',dict(sha='anchor',date='date'))
    row=dict(index=1,sha='sha',date='date',subject='change',**{'changes-src-main':'yes','changes-pom':'no'})
    write_csv(tmp_path/'results/commits.csv',[row],list(row))
    write_json(tmp_path/'results/commit-paths.json',{'1':['src/main/java/T.java']})
    write_json(tmp_path/'results/builds/1.json',dict(sha='sha',build='ok'))
    write_json(tmp_path/'results/workspace-identities/1.json',dict(sha='sha',dependencies=[]))
    checkout=tmp_path/'work/checkouts/1';checkout.mkdir(parents=True)
    (checkout/'source.java').write_text('evidence')
    write_json(tmp_path/'work/checkouts/1.json',dict(sha='sha'))
    barrier=threading.Barrier(2);fast_validated=threading.Event()
    monkeypatch.setattr(survive,'measurement_session',lambda *args:nullcontext())
    def log(exp,message,*args):
        if message=='survive: 1 fast recorded':fast_validated.set()
    monkeypatch.setattr(survive,'log',log)
    def worker(self,job):
        barrier.wait(timeout=5)
        if job['group']=='slow':
            assert fast_validated.wait(timeout=5)
            assert (checkout/'source.java').read_text()=='evidence'
            if fail_slow:raise RuntimeError('interrupted consumer')
        result=dict(index=1,sha='sha',group=job['group'],status='evaluated',files=[],
                    methods=[dict(method,state='pass',flaky=False)])
        write_json(tmp_path/job['output'],result)
        return result
    monkeypatch.setattr(JavaMavenAdapter,'worker',worker)
    if fail_slow:
        with pytest.raises(RuntimeError,match='interrupted consumer'):survive.run(tmp_path,config)
        assert (checkout/'source.java').read_text()=='evidence'
    else:
        survive.run(tmp_path,config)
        assert checkout.exists() == needs_coverage
        assert (tmp_path/'work/checkouts/1.json').exists() == needs_coverage


def test_pool_cleanup_requires_verified_immutable_snapshots(tmp_path):
    from test_survival.policy.pools import verified_pool
    from test_survival.policy.artifacts import tree_hash
    from test_survival.config import canonical_hash
    import hashlib
    root=tmp_path/'results/policy/pools/dev/2'
    source=root/'dev/src-test-t/java/T.java';source.parent.mkdir(parents=True)
    source.write_text('class T {}')
    assert not verified_pool(tmp_path,root,[])
    write_json(root/'results/workspace-identities/0.json',dict(sha='sha',dependencies=[]))
    source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    sources=tree_hash(source.parent,'*.java')
    resources=canonical_hash({name:tree_hash(root/'dev/src-test-t'/name) for name in ('resources','suite')})
    member=dict(file=str(source.relative_to(tmp_path)),file_hash=source_hash,
                source_root=str(source.parent.relative_to(tmp_path)),source_tree_hash=sources,
                resource_root=str((root/'dev/src-test-t').relative_to(tmp_path)),resource_tree_hash=resources,
                execution_hash=canonical_hash(dict(file=source_hash,sources=sources,resources=resources)))
    assert verified_pool(tmp_path,root,[member])
    source.write_text('class T { int changed; }')
    with pytest.raises(RuntimeError,match='snapshot changed'):verified_pool(tmp_path,root,[member])
    assert source.exists()


def test_survive_and_cover_build_once_including_predecessor_alias(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from test_survival.io import write_csv
    from test_survival.stages import survive, cover
    adapter, config, builds = adapter_fixture(tmp_path, monkeypatch)
    config['range']['skip_untouched'] = 'main-sources'
    config['dev']['groups'] = [dict(name='dev', denominator='project')]
    commits = [dict(index=i, sha=str(i), date='date', subject='change',
                    **{'changes-src-main':'yes' if i == 3 else 'no', 'changes-pom':'yes' if i in (1,4) else 'no'})
               for i in range(1,5)]
    method = dict(id='T#test', file='T.java', execution_hash='source', **{'class':'T'}, method='test', kind='plain')
    write_csv(tmp_path/'results/commits.csv', commits, list(commits[0]))
    write_json(tmp_path/'results/anchor.json', dict(sha='0', date='date'))
    write_json(tmp_path/'results/commit-paths.json', {'1':['pom.xml'], '2':['README.md'], '3':['src/main/java/A.java'], '4':['pom.xml']})
    write_json(tmp_path/'results/frozen-denominators.json', {'dev':[method]})
    write_json(tmp_path/'population/scope.json', dict(classes=[dict(file='src/main/java/A.java')]))
    # The successful prepare builds, retained until the final consumer.
    for i in (0,1,3,4):adapter.build(i,str(i))
    for stage in (survive, cover):
        monkeypatch.setattr(stage, 'JavaMavenAdapter', lambda *args:adapter)
        monkeypatch.setattr(stage, 'measurement_session', lambda *args:nullcontext())
    monkeypatch.setattr(adapter, 'git', lambda *args:'--- a/src/main/java/A.java\n+++ b/src/main/java/A.java\n@@ -1 +1 @@\n-old\n+new')
    def worker(job):
        adapter.build(job['index'], job['sha'])
        adapter.build(0, '0')
        result = dict(index=job['index'], sha=job['sha'], group='dev', status='evaluated', files=[],
                      methods=[dict(t, state='pass', flaky=False) for t in job['methods']])
        write_json(tmp_path/job['output'], result)
        if job['phase'] == 'B':
            write_csv(tmp_path/f'results/coverage/{job["index"]}-dev-lines.csv',
                      [dict(file='src/main/java/A.java', line=1, covered='yes')], ['file','line','covered'])
        return result
    monkeypatch.setattr(adapter, 'worker', worker)
    survive.run(tmp_path, config)
    assert all(adapter.folder(i).exists() for i in (0,1,3))
    assert not adapter.folder(4).exists()
    cover.run(tmp_path, config)
    assert read_json(tmp_path/'results/coverage-index-aliases.json') == {'2':1}
    assert adapter.folder(0).exists()
    assert all(not adapter.folder(i).exists() for i in (1,3,4))
    assert len(builds) == 4
    assert not (tmp_path/'results/workspace-restorations').exists()
    finish_checkouts(tmp_path, config)
    assert not (tmp_path/'work/checkouts').exists()


def test_verification_counts_all_restoration_attempts_including_pools(tmp_path):
    import runpy
    metrics = runpy.run_path(str(Path(__file__).parents[1]/'scripts/footprint_metrics.py'))
    count = metrics['restoration_counts']
    assert count(tmp_path) == dict(total=0, by_experiment={})
    for name, attempts in [('baseline', 2), ('policy/results/policy/pools/dev/2', 1)]:
        root = tmp_path/name/'results/workspace-restorations/3'
        # Count completed, failed and empty interrupted attempts alike.
        for i in range(attempts):
            (root/str(i)).mkdir(parents=True)
        write_json(root/'0/result.json', dict(build='ok'))
    assert count(tmp_path) == dict(total=3, by_experiment={'baseline':2, 'policy/results/policy/pools/dev/2':1})
