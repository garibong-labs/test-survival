import importlib.util
from pathlib import Path
import subprocess

import yaml


def test_dns_verification_preserves_original_retry_settings(tmp_path, monkeypatch):
    scripts=Path(__file__).parents[1]/'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('affected_compare',scripts/'affected_selection_compare.py')
    compare=importlib.util.module_from_spec(spec);spec.loader.exec_module(compare)
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    source=tmp_path/'_projects/experiment-projects/dnsjava/experiments/commit-survival'
    generated=source/'generated/ouedraogo';generated.mkdir(parents=True)
    (generated/'frozen.java').write_text('class Frozen {}')
    retry=dict(flaky_rerun=1,environment_errors=dict(patterns=['java.net.BindException'],max_reruns=3,backoff_seconds=5))
    original=dict(anchor='abc',project=dict(repo='original'),range=dict(limit=178),
        generators=[dict(name='ouedraogo'),dict(name='ktester')],model=dict(name='test'),
        runner=dict(docker=dict(image='test'),workers=dict(build=4,survive=4,cover=4)),survival=retry)
    (source/'experiment.yaml').write_text(yaml.safe_dump(original))
    def git(args,**kwargs):
        if args[1]=='clone':Path(args[-1]).mkdir(parents=True)
        return subprocess.CompletedProcess(args,0)
    monkeypatch.setattr(subprocess,'run',git)
    compare.setup(tmp_path/'verification','dnsjava')
    for mode in ('all','affected'):
        saved=yaml.safe_load((tmp_path/f'verification/repo/experiments/{mode}/experiment.yaml').read_text())
        assert saved['survival']==dict(retry,execution='batch',selection=mode)
        assert saved['runner']['workers']==dict(build=1,survive=1,cover=1)
        assert saved['range']['limit']==60


def test_resume_preserves_successful_stage_wall_time_and_log(tmp_path, monkeypatch):
    import json
    scripts=Path(__file__).parents[1]/'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('affected_compare_resume',scripts/'affected_selection_compare.py')
    compare=importlib.util.module_from_spec(spec);spec.loader.exec_module(compare)
    log=tmp_path/'all-prepare-scope.log';log.write_text('original completed preparation\n')
    timing=tmp_path/'all-timing.json'
    timing.write_text(json.dumps(dict(tool_head='old',steps=[dict(stage='prepare-scope',seconds=12,exit_code=0,log=str(log))])))
    commands=[]
    class Child:
        def wait(self,timeout):return 0
    def launch(command,**kwargs):
        commands.append(command);kwargs['stdout'].write('fresh measurement\n');return Child()
    monkeypatch.setattr(subprocess,'Popen',launch)
    monkeypatch.setattr(subprocess,'check_output',lambda *args,**kwargs:'new\n')
    compare.run(tmp_path,'all',tmp_path/'tool',resume=True)
    result=json.loads(timing.read_text())
    assert log.read_text()=='original completed preparation\n'
    assert result['steps'][0]['seconds']==12 and result['steps'][0]['tool_head']=='old'
    assert all(s['tool_head']=='new' for s in result['steps'][1:])
    assert result['seconds']>=12
    assert len(commands)==6 and commands[0][3]=='import'
    assert list(tmp_path.glob('all-timing.before-resume-*.json'))
