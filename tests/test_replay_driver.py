"""Exercise migration sequencing without running experiment measurements."""
import importlib
from pathlib import Path
import sys

import pytest

from test_survival.config import STAGES, load, stage_hash
from test_survival.io import read_json, write_json
from test_survival.pipeline import run


@pytest.mark.parametrize('active_stage', ['survive', 'cover'])
def test_driver_migrates_before_resuming_and_preserves_completed_cover(tmp_path, monkeypatch, active_stage):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'scripts'))
    driver = importlib.import_module('run_replay_comparisons')
    root = tmp_path/'tool'
    (root/'tests/evidence').mkdir(parents=True)
    monkeypatch.setattr(driver, '__file__', str(root/'scripts/run_replay_comparisons.py'))
    replay, serial, reference = [tmp_path/name for name in ['replay', 'serial', 'reference']]
    replay.mkdir()
    (replay/'experiment.yaml').write_text('name: test\nproject: {repo: /project}\nanchor: t\nrunner:\n  workers: {survive: 4}\n')
    for stage in STAGES[:STAGES.index(active_stage)+1]:
        write_json(replay/f'state/{stage}.json', {'status': 'completed', 'config_hash': 'old', 'notes': 'retained'})
    write_json(replay/'state/run.json', {'status': 'running', 'pid': 123})
    monkeypatch.setattr(driver, 'process_alive', lambda pid: True)
    monkeypatch.setattr(driver.time, 'sleep', lambda seconds: write_json(replay/'state/run.json', {'status': 'completed'}))
    calls = []

    def command(argv, **kwargs):
        if '-m' not in argv:
            return  # Comparison commands do not launch a measurement.
        assert argv[3] == 'run'
        exp = Path(argv[4])
        stage = argv[argv.index('--stage')+1]
        calls.append((exp.name, stage, '--force' in argv))
        run(exp, load(exp), stage=stage, force='--force' in argv,
            handlers={name: lambda exp, config: 'test measurement' for name in STAGES})

    monkeypatch.setattr(driver.subprocess, 'run', command)
    monkeypatch.setattr(sys, 'argv', ['driver', '--replay', str(replay), '--reference', str(reference), '--serial', str(serial)])
    driver.main()
    expected = [('replay', 'cover', False)] if active_stage == 'survive' else []
    assert calls == expected + [('replay', 'report', False), ('serial', 'survive', True), ('serial', 'cover', True), ('serial', 'report', True)]
    for exp in [replay, serial]:
        for stage in STAGES:
            assert read_json(exp/f'state/{stage}.json')['config_hash'] == stage_hash(load(exp), stage)
    migrations = read_json(root/'tests/evidence/state-hash-migration.json')['migrations']
    assert [entry['experiment'] for entry in migrations] == [str(replay), str(replay), str(serial)]
    assert read_json(root/'tests/evidence/comparison-driver.json')['status'] == 'completed'
