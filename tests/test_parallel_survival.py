"""Real thread scheduling with mocked measurements; no experiment execution."""
from contextlib import nullcontext
from copy import deepcopy
import shutil
import threading

from test_policy import baseline, test_test
from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import validate
from test_survival.io import read_csv, read_json, write_csv, write_json
from test_survival.policy import execute, cover as policy_cover
from test_survival.policy.baseline import Baseline
from test_survival.policy.pipeline import Steps
from test_survival.stages import survive, cover


class LaggingWorkers:
    """Four start together; the second finishes before the first.

    Its replacement (fifth) must see the second's failure, but cannot yet know
    the first's failure. Events keep this deterministic without timing sleeps.
    """
    def __init__(self, first):
        self.first=first; self.barrier=threading.Barrier(4); self.fifth=threading.Event()
        self.lock=threading.Lock(); self.active=0; self.peak=0

    def enter(self, index):
        with self.lock:
            self.active+=1; self.peak=max(self.peak,self.active)
        if self.first <= index < self.first+4:
            self.barrier.wait(timeout=10)
            if index != self.first+1:
                assert self.fifth.wait(timeout=10)
        if index==self.first+4:
            self.fifth.set()

    def leave(self):
        with self.lock:
            self.active-=1


def test_four_baseline_workers_preserve_aggregate_and_coverage_bytes(tmp_path,monkeypatch):
    methods=[dict(id=n,file='T.java',execution_hash='source',**{'class':'T'},method=n,kind='plain') for n in 'abc']
    config=validate(dict(name='parallel',anchor='t',project={'repo':str(tmp_path)},dev={'groups':[{'name':'dev'}]}))
    commits=[dict(index=i,sha=str(i),date='date',subject='change',**{'changes-src-main':'yes','changes-pom':'no'}) for i in range(1,7)]
    monkeypatch.setattr(survive,'measurement_session',lambda *a:nullcontext())
    monkeypatch.setattr(cover,'measurement_session',lambda *a:nullcontext())
    monkeypatch.setattr(JavaMavenAdapter,'git',lambda *a:'--- a/src/main/java/A.java\n+++ b/src/main/java/A.java\n@@ -1 +1 @@\n-old\n+new')
    outputs={}; jobs={}; gates={}
    def worker(self,job):
        jobs[self.exp.name].append(deepcopy(job))
        gate=gates.get(self.exp.name)
        if gate:gate.enter(job['index'])
        observed=[dict(t,state='fail' if (job['index'],t['id']) in ((1,'a'),(2,'b')) else 'pass',flaky=False)
                  for t in job['methods']]
        result=dict(index=job['index'],sha=job['sha'],group='dev',status='evaluated',files=[],
                    input_sha256=job['input_sha256'],runner_sha256=job['runner_sha256'],methods=observed+job['dead_methods'])
        write_json(self.exp/job['output'],result)
        if gate:gate.leave()
        return result
    def coverage(self,index,files,selected,group):
        rows=[dict(file='src/main/java/A.java',line=1,covered='yes' if any(t['id']=='c' for t in selected) else 'no')]
        write_csv(self.exp/f'results/coverage/{index}-{group}-lines.csv',rows,list(rows[0]))
        return {'status':'evaluated'}
    monkeypatch.setattr(JavaMavenAdapter,'worker',worker)
    monkeypatch.setattr(JavaMavenAdapter,'coverage',coverage)
    for name,workers in [('sequential',1),('parallel',4)]:
        exp=tmp_path/name; jobs[name]=[]
        if workers==4:gates[name]=LaggingWorkers(1)
        config['runner']['workers']['survive']=workers
        write_json(exp/'results/frozen-denominators.json',{'dev':methods})
        write_json(exp/'results/anchor.json',dict(sha='anchor',date='date'))
        write_csv(exp/'results/commits.csv',commits,list(commits[0]))
        write_json(exp/'results/commit-paths.json',{str(i):['src/main/java/A.java'] for i in range(1,7)})
        write_json(exp/'population/scope.json',dict(classes=[dict(file='src/main/java/A.java')]))
        for row in commits:write_json(exp/f'results/builds/{row["index"]}.json',dict(sha=row['sha'],build='ok'))
        survive.run(exp,config); cover.run(exp,config)
        outputs[name]={n:(exp/'results'/n).read_bytes() for n in
                       ('survival.csv','first-failures.csv','alive-entering.json','changed-lines.csv','coverage-consistency.json')}
    assert outputs['parallel']==outputs['sequential']
    assert gates['parallel'].peak==4
    requests={j['index']:{t['id'] for t in j['methods']} for j in jobs['parallel']}
    assert all(requests[i]==set('abc') for i in range(1,5))
    assert requests[5]==set('ac')  # b died at completed 2; a's index 1 is still running.
    exp=tmp_path/'parallel'; before={p:p.read_bytes() for p in (exp/'results/raw').glob('*.json')}
    config['runner']['workers']['survive']=1
    survive.run(exp,config)
    assert len(jobs['parallel'])==6 and before=={p:p.read_bytes() for p in before}
    # A sequential cache with more dead markers waits for the missing earlier
    # record, rather than being needlessly remeasured with a lagging dead set.
    exp=tmp_path/'sequential'; prior=(exp/'results/raw/2-dev.json').read_bytes()
    (exp/'results/raw/1-dev.json').unlink(); config['runner']['workers']['survive']=4
    count=len(jobs['sequential']); survive.run(exp,config)
    assert len(jobs['sequential'])==count+1
    assert (exp/'results/raw/2-dev.json').read_bytes()==prior


def test_four_policy_workers_preserve_curves_coverage_and_regeneration(baseline,monkeypatch):
    exp,config,root=baseline; b=Baseline(exp,config)
    b.options['set']=[dict(name='early',trigger='release',action='reset'),dict(name='late',trigger='threshold',action='fill')]
    for i in range(4,10):
        row={**b.curves[-1],'index':str(i),'sha':str(i)}; b.curves.append(row); b.by_index[i]=row
    plan=dict(points=[dict(group='dev-scope',index=1,trigger='release'),dict(group='dev-scope',index=7,trigger='threshold')],
              pools=[dict(group='dev-scope',index=i) for i in (1,7)])
    tests=[dict(test_test(n),execution_hash='shared') for n in 'abc']
    pools={('dev-scope',i):tests for i in (1,7)}
    monkeypatch.setattr(execute,'baseline_tests',lambda _: {'dev-scope':[]})
    monkeypatch.setattr(b,'raw',lambda *a:{'methods':[]})
    monkeypatch.setattr(JavaMavenAdapter,'build',lambda *a:{'build':'ok'})
    monkeypatch.setattr(JavaMavenAdapter,'setup_runner',lambda *a:None)
    rows=[dict(index=i,sha=b.by_index[i]['sha'],date='date',subject='change',scope_executable=1,
               **{'scope_dev-scope_covered':0}) for i in range(1,10)]
    write_csv(root/'results/changed-lines.csv',rows,list(rows[0]))
    for i in range(1,10):write_json(root/f'results/changes/{i}.json',dict(post_image={'src/main/java/A.java':[1]}))
    gate=None; jobs=[]; completed=set(); lock=threading.Lock()
    def worker(self,job):
        with lock:
            jobs.append(deepcopy(job))
            if job['index']>=8:assert set(range(2,7)) <= completed
        if gate and job['index']<=6:gate.enter(job['index'])
        methods=[]
        for t in job['methods']:
            fail=(job['index'],t['id']) in ((2,'a'),(3,'b'))
            # Coverage missing only for an extra, already-dead execution must
            # not mark the actual alive-entry policy measurement unavailable.
            missing=job['index']==6 and t['id']=='a'
            methods.append(dict(t,state='fail' if fail or missing else 'pass',
                                **({} if missing else {'lines':[['src/main/java/A.java',1]]})))
        with lock:completed.add(job['index'])
        if gate and job['index']<=6:gate.leave()
        return dict(methods=methods,coverage_reason='batch JVM failed or timed out')
    monkeypatch.setattr(JavaMavenAdapter,'worker',worker)
    def output():
        return {(p,n):(exp/f'results/policy/{p}/{n}').read_bytes() for p in ('early','late') for n in
                ('survival.csv','first-failures.csv','alive.json','coverage-commits.csv','coverage-total.json','coverage-missing.csv')}
    b.execution_config['runner']['workers']['survive']=1
    execute.survive(b,plan,pools,Steps(b,plan)); policy_cover.run(b,None); expected=output()
    shutil.rmtree(exp/'results/policy')
    for path in (exp/'state').glob('policy-*.json'):path.unlink()
    jobs.clear(); completed.clear(); gate=LaggingWorkers(2)
    b.execution_config['runner']['workers']['survive']=4
    execute.survive(b,plan,pools,Steps(b,plan)); policy_cover.run(b,None)
    assert output()==expected and gate.peak==4
    requested={j['index']:{t['id'] for t in j['methods']} for j in jobs}
    assert all(requested[i]==set('abc') for i in range(2,6))
    assert requested[6]==set('ac')
    assert requested[8]==set('abc')  # The late policy revives both dead methods.
    before={p:p.read_bytes() for p in (exp/'results/policy/runs').glob('*/*.json')}
    count=len(jobs); b.execution_config['runner']['workers']['survive']=1
    execute.survive(b,plan,pools,Steps(b,plan)); policy_cover.run(b,None)
    assert len(jobs)==count and before=={p:p.read_bytes() for p in before}
    assert output()==expected
