from copy import deepcopy

import pytest

from test_survival.config import validate, stage_hash, generator_config
from test_survival.io import read_json, write_json
from test_survival.work import cleanup_stage, finish_work, retain_previous_work


def fixture(tmp_path, *, failure=False):
    config=validate(dict(name='scratch',anchor='t',project={'repo':str(tmp_path)}))
    for number in (0,1):
        p=tmp_path/f'work/phase-A/2/dev/{number}/classes/T.class';p.parent.mkdir(parents=True);p.write_bytes(b'class')
        p.with_name('javac.txt').write_text('original diagnostics')
    write_json(tmp_path/'work/phase-A/2/dev-job.json',dict(action='run',work='work/phase-A/2/dev',output='results/raw/2-dev.json'))
    write_json(tmp_path/'results/raw/2-dev.json',dict(files=[dict(methods=[dict(state='fail' if failure else 'pass')]),dict(methods=[dict(state='pass')])]))
    return config


def test_success_and_failed_file_subtree_cleanup(tmp_path):
    config=fixture(tmp_path,failure=True)
    failed=tmp_path/'work/phase-A/2/dev/0';before={p:p.read_bytes() for p in failed.rglob('*') if p.is_file()}
    cleanup_stage(tmp_path,config,'survive')
    assert before=={p:p.read_bytes() for p in before}
    assert not (tmp_path/'work/phase-A/2/dev/1').exists()
    metrics=read_json(tmp_path/'results/work-cleanup.json')
    assert metrics['removed_files']==2
    assert metrics['retained_failures']['work/phase-A/2/dev/0']['files']==2
    cleanup_stage(tmp_path,config,'survive')
    assert read_json(tmp_path/'results/work-cleanup.json')['removed_files']==2


def test_missing_result_is_never_cleaned(tmp_path):
    config=fixture(tmp_path)
    (tmp_path/'results/raw/2-dev.json').unlink()
    cleanup_stage(tmp_path,config,'survive')
    assert (tmp_path/'work/phase-A/2/dev/0/classes/T.class').exists()


def test_keep_work_is_operational_and_survives_generator_copy(tmp_path):
    config=fixture(tmp_path)
    hashes=[stage_hash(config,s) for s in ('prepare','generate','survive')]
    config.keep_work=True
    assert generator_config(config,{}).keep_work
    assert deepcopy(config).keep_work
    assert hashes==[stage_hash(config,s) for s in ('prepare','generate','survive')]
    cleanup_stage(tmp_path,config,'survive');finish_work(tmp_path,config)
    assert (tmp_path/'work/phase-A/2/dev/0/classes/T.class').exists()


def test_histories_cache_generated_and_completion_preserved(tmp_path):
    config=fixture(tmp_path)
    protected=['work/compile-cache/v1/entry/classes/X.class','work/generation-history/old/generated/result.json',
               'work/policy-history/old/generated/result.json','generated/x/result.json',
               'work/ktester/x/experiment-result.json']
    for name in protected:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'protected')
    p=tmp_path/'work/ktester/x/projects/source.java';p.parent.mkdir();p.write_text('scratch')
    before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (tmp_path/n for n in protected)}
    finish_work(tmp_path,config)
    assert before=={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in before}
    assert not (tmp_path/'work/ktester/x/projects').exists()


def test_resumed_worker_preserves_interrupted_attempt(tmp_path):
    config=fixture(tmp_path)
    joblog=tmp_path/'work/phase-A/2/dev-job.log';joblog.write_text('interrupted worker')
    retain_previous_work(tmp_path,'work/phase-A/2/dev')
    assert (tmp_path/'work/phase-A/2/dev').exists()
    # The worker clears the original tree; the snapshot has independent inodes.
    import shutil
    shutil.rmtree(tmp_path/'work/phase-A/2/dev')
    attempts=list((tmp_path/'work/failed-attempts/phase-A/2/dev').glob('*/0/classes/T.class'))
    assert len(attempts)==1 and attempts[0].read_bytes()==b'class'
    finish_work(tmp_path,config)
    assert attempts[0].exists()
    preserved=list((tmp_path/'work/failed-attempts/phase-A/2/dev').glob('*-job.log'))
    joblog.write_text('resumed worker')
    assert len(preserved)==1 and preserved[0].read_text()=='interrupted worker'


def test_background_forwarding(tmp_path,monkeypatch):
    from test_survival import pipeline
    calls=[]
    monkeypatch.setattr(pipeline,'launch_background',lambda exp,args: calls.append(args))
    pipeline.background(tmp_path,keep_work=True)
    assert '--keep-work' in calls[0]
    from test_survival.cli import parser
    assert parser().parse_args(['run',str(tmp_path),'--keep-work']).keep_work
    assert parser().parse_args(['policy','run',str(tmp_path),'--keep-work']).keep_work


def test_retry_preserves_job_failure_even_without_a_work_directory(tmp_path):
    log=tmp_path/'work/phase-A/2/dev-job.log';log.parent.mkdir(parents=True)
    log.write_text('worker failed before creating scratch')
    retain_previous_work(tmp_path,'work/phase-A/2/dev')
    log.write_text('retry log')
    saved=list((tmp_path/'work/failed-attempts/phase-A/2/dev').glob('*-job.log'))
    assert len(saved)==1 and saved[0].read_text()=='worker failed before creating scratch'


def test_pool_cleanup_uses_supplied_runner_without_yaml(tmp_path, monkeypatch):
    from test_survival import work
    from test_survival.runner.docker import DockerRunner
    config = fixture(tmp_path, failure=False)
    config['runner']['docker']['image'] = 'pool-specific-image'
    assert not (tmp_path/'experiment.yaml').exists()
    original = work._remove_owned
    monkeypatch.setattr(work.os, 'geteuid', lambda:1000)
    monkeypatch.setattr(work, '_remove_owned', lambda *args: (_ for _ in ()).throw(PermissionError('container owner')))
    calls = []
    def execute(runner, command, cwd, log, timeout):
        assert runner.config is config
        assert runner.config['runner']['docker']['image'] == 'pool-specific-image'
        calls.append(command[-1])
        original(tmp_path, command[-1])
        return dict(exit_code=0)
    monkeypatch.setattr(DockerRunner, 'execute', execute)
    for name in ('work/checkouts/0/target/classes/A.class', 'work/libs/runtime.jar'):
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'compiled')
    write_json(tmp_path/'work/checkouts/0.json', dict(sha='sha'))
    write_json(tmp_path/'results/builds/0.json', dict(sha='sha',build='ok'))
    write_json(tmp_path/'results/workspace-identities/0.json', dict(sha='sha'))
    work.finish_checkouts(tmp_path, config)
    work.finish_work(tmp_path, config, pool=True)
    assert not (tmp_path/'work/checkouts').exists()
    assert not (tmp_path/'work/phase-A/2/dev').exists()
    assert not (tmp_path/'work/libs').exists()
    assert 'work/checkouts/0' in calls and 'work/libs' in calls
    assert len(list((tmp_path/'results/work-cleanup-commands').glob('*.json'))) == len(calls)


def test_pool_build_and_cleanup_configuration_keeps_generator_override(tmp_path):
    from types import SimpleNamespace
    from test_survival.policy.pools import pool_config
    config = validate(dict(name='pool', anchor='t', project={'repo':str(tmp_path)},
                           runner={'docker':{'image':'baseline-image'}},
                           generators=[dict(name='llm',method='ouedraogo',impl='native',docker={'image':'generator-image'})]))
    config.keep_work=True
    config['runner']['policy_read_only']=[str(tmp_path/'baseline')]
    effective=pool_config(SimpleNamespace(execution_config=config),dict(group='llm',sha='pool-sha'))
    assert effective['runner']['docker']['image']=='generator-image'
    assert effective['runner']['policy_read_only']==[str(tmp_path/'baseline')]
    assert effective['anchor']=='pool-sha' and effective.keep_work
    assert effective['runner']['reuse']==dict(checkouts=None,classpath_cache=None)
    assert config['runner']['docker']['image']=='baseline-image' and config['anchor']=='t'
