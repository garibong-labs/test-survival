"""KTester lifecycle after cleanup, without author-tool execution or model calls."""
from contextlib import contextmanager
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from test_survival.config import stage_hash, validate
from test_survival.generators import completion, ktester_tool
from test_survival.io import read_json, write_csv, write_json
from test_survival.work import finish_work


def snapshot(root):
    return {str(p.relative_to(root)):(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_ctime_ns)
            for p in root.rglob('*') if p.is_file()}


@pytest.fixture
def completed(tmp_path, monkeypatch):
    config=validate(dict(name='ktester-cleanup',anchor='t',project={'repo':str(tmp_path)},
                         runner={'docker':{'image':'fixture-only'}},
                         generators=[dict(name='ktester',method='ktester',impl='author-tool',tool_path='/unused',
                                          rounds={'max':1,'until_dev_matched':False})]))
    dataset=tmp_path/'work/ktester/ktester'
    for action in ('prepare','index','dataset','generate-round1'):
        write_json(dataset/(action+'.json'),dict(status='completed',exit_code=0))
    write_json(dataset/'dataset_info.json',dict(project={'focal-methods':[]}))
    for path in (dataset/'projects/source.java',dataset/'project_index/index'):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text('regenerable')
    work=dataset/'rounds/round1'
    write_json(work/'run.json',dict(status='completed',round=1,model_calls=1))
    write_json(work/'raw-responses/0001/call.json',dict(status='ok',seconds=1))
    write_json(work/'raw-responses/0001/request.json',dict(model='fixture',messages=[{'content':'original prompt'}]))
    write_json(work/'raw-responses/0001/response.json',dict(content='class Test {}'))
    source=work/'project/test_classes/Test.java';source.parent.mkdir(parents=True);source.write_text('class Test {}')
    archive=tmp_path/'generated/ktester/round1'
    shutil.copytree(work,archive)
    (archive/'sources').mkdir();shutil.copy2(source,archive/'sources/Test.java')
    write_json(tmp_path/'generated/ktester/rounds.json',dict(config_hash=stage_hash(config,'generate','ktester'),stopped='max-rounds'))
    write_json(tmp_path/'state/generate.json',dict(status='completed',config_hash=stage_hash(config,'generate')))
    write_json(tmp_path/'results/anchor.json',dict(sha='anchor'))
    summary=dict(status='completed',denominators={'ktester':1},model_rounds={'ktester':1},
                 phase_seconds={'generate':1},final_verification={'valid':True},finished='before')
    write_json(tmp_path/'results/final-summary.json',summary)
    monkeypatch.setattr(completion.subprocess,'check_output',lambda args,**kw:'branch\n' if args[-1]=='--show-current' else 'commit\n')
    completion.record(tmp_path,config,summary)
    monkeypatch.setattr(ktester_tool,'launch',lambda *args,**kw:pytest.fail('Completed round launched a worker'))
    monkeypatch.setattr(ktester_tool,'_read',lambda *args:pytest.fail('Completed round read credentials'))
    return tmp_path,config,dataset,archive,summary


@pytest.mark.parametrize('pool',[False,True])
def test_completed_round_reuses_archive_after_cleanup(completed,pool):
    exp,config,dataset,archive,_=completed
    before=snapshot(archive)
    finish_work(exp,config,pool=pool)
    assert not (dataset/'projects').exists() and not (dataset/'project_index').exists()
    assert not (dataset/'rounds').exists()
    result=ktester_tool.KTesterTool(exp,config,config['generators'][0]).generate(1)
    assert result.files==[archive/'project/test_classes/Test.java']
    assert result.calls==[dict(status='ok',seconds=1)]
    assert snapshot(archive)==before
    assert (dataset/'dataset_info.json').is_file()


def test_raising_round_limit_only_executes_the_new_round(completed,monkeypatch):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.runner.docker import DockerRunner
    from test_survival.stages import generate
    exp,config,dataset,archive,_=completed
    finish_work(exp,config)
    before=snapshot(archive)
    config['generators'][0]['rounds']['max']=2
    write_csv(exp/'dev/dev-denominator.csv',[dict(id='dev')],['id'])
    monkeypatch.setattr(JavaMavenAdapter,'setup_runner',lambda self:None)
    monkeypatch.setattr(DockerRunner,'command',lambda self,command,cwd:['docker','run','fixture-only',*command])
    monkeypatch.setattr(ktester_tool,'_read',lambda config:'fixture-only-key')
    monkeypatch.setattr(generate,'screen_round',lambda *args:([dict(id='kept')],dict(compiled=1)))
    monkeypatch.setattr(generate,'log',lambda *args:None)
    actions=[]
    @contextmanager
    def launch(exp,command,**kwargs):
        job=read_json(Path(command[-1]));actions.append((job['round'],job['action']))
        if job['action']=='generate':
            new=exp/'generated/ktester'/job['label']
            write_json(new/'run.json',dict(status='completed',round=job['round']))
            for name,content in [('project/test_classes/Test.java','class Test { int generated; }'),
                                 ('sources/Test.java','class Test { int snapshot; }')]:
                path=new/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
        yield SimpleNamespace(wait=lambda:None,returncode=0)
    monkeypatch.setattr(ktester_tool,'launch',launch)
    generate.run(exp,config)
    assert actions==[(2,action) for action in ('prepare','index','dataset','generate')]
    assert snapshot(archive)==before
    assert (exp/'generated/ktester/round2/sources/Test.java').read_text()=='class Test { int generated; }'
    assert [r['round'] for r in read_json(exp/'generated/ktester/rounds.json')['rounds']]==[1,2]


@pytest.mark.parametrize('old_cleanup',[False,True])
def test_report_refresh_updates_completion_after_cleanup(completed,old_cleanup,monkeypatch):
    import hashlib
    from test_survival.config import STAGES
    from test_survival import pipeline
    from test_survival.stages import report
    exp,config,dataset,_,summary=completed
    finish_work(exp,config)
    if old_cleanup:
        (dataset/'dataset_info.json').unlink(missing_ok=True)
    updated={**summary,'finished':'after','denominators':{'ktester':2},'final_survivors':{'ktester':1},
             'final_verification':{'valid':False}}
    def summarize(*args):
        write_json(exp/'results/final-summary.json',updated)
        return updated
    for stage in STAGES:
        write_json(exp/'state'/f'{stage}.json',dict(status='completed',config_hash=stage_hash(config,stage)))
    monkeypatch.setattr(report.tables,'build',lambda *args:None)
    monkeypatch.setattr(report.figures,'build',lambda *args:None)
    monkeypatch.setattr(report.figures,'png',lambda *args:False)
    monkeypatch.setattr(report.consistency,'build',summarize)
    monkeypatch.setattr(pipeline,'log',lambda *args:None)
    pipeline.run(exp,config,stage='report',force=True,no_panel=True)
    saved=read_json(dataset/'experiment-result.json')
    assert saved['final_summary_sha256']==hashlib.sha256((exp/'results/final-summary.json').read_bytes()).hexdigest()
    assert saved['denominators']==updated['denominators'] and saved['verification']==updated['final_verification']
    assert read_json(exp/'state/report.json')['status']=='completed'


def test_import_without_author_dataset_does_not_create_completion(tmp_path,monkeypatch):
    config=validate(dict(name='imported',anchor='t',project={'repo':str(tmp_path)},
                         generators=[dict(name='ktester',method='ktester',impl='author-tool',tool_path='/unused')]))
    monkeypatch.setattr(completion.subprocess,'check_output',lambda *args,**kw:pytest.fail('Imported generation has no dataset'))
    completion.record(tmp_path,config,{})
    assert not (tmp_path/'work/ktester').exists()


def test_completed_archive_can_finish_missing_flat_copy(completed):
    exp,config,_,archive,_=completed
    finish_work(exp,config)
    (archive/'sources/Test.java').unlink()  # Interrupted after archival, before flattening.
    before=snapshot(archive)
    ktester_tool.KTesterTool(exp,config,config['generators'][0]).generate(1)
    assert (archive/'sources/Test.java').read_bytes()==(archive/'project/test_classes/Test.java').read_bytes()
    after=snapshot(archive)
    assert all(after[name]==value for name,value in before.items())


@pytest.mark.parametrize('status',['failed',None])
def test_unverified_archive_never_reenters_generation(completed,status):
    exp,config,dataset,archive,_=completed
    if status:write_json(archive/'run.json',dict(status=status))
    else:(archive/'run.json').unlink()
    before=snapshot(archive)
    work=snapshot(dataset)
    finish_work(exp,config)
    assert snapshot(dataset)==work
    with pytest.raises(RuntimeError,match='archive'):
        ktester_tool.KTesterTool(exp,config,config['generators'][0]).generate(1)
    assert snapshot(archive)==before
