import pytest
import yaml

from test_survival.cli import main
from test_survival.config import STAGES, load, stage_hash
from test_survival.io import read_json, write_json
from test_survival.pipeline import PipelineError, rehash, run_lock


def files(exp):
    return {str(p.relative_to(exp)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in exp.rglob('*') if p.is_file()}


def test_rehash_changes_only_completed_stage_config_hashes(tmp_path, capsys):
    (tmp_path/'experiment.yaml').write_text(yaml.safe_dump(dict(name='migration', project={'repo': '/unused'}, anchor='t')))
    for i, stage in enumerate(STAGES):
        write_json(tmp_path/f'state/{stage}.json', dict(status=['completed', 'failed', 'completed', 'pending'][i%4],
                   config_hash='old', started='start', finished='finish', notes='retained', group_hashes={'k': 'old-group'}))
    write_json(tmp_path/'state/run.json', {'status': 'completed', 'config_hash': 'unchanged'})
    write_json(tmp_path/'state/import.json', {'source': 'unchanged'})
    write_json(tmp_path/'results/evidence.json', {'config_hash': 'unchanged'})
    write_json(tmp_path/'generated/k/rounds.json', {'config_hash': 'unchanged'})
    (tmp_path/'state/log.txt').write_text('unchanged log\n')
    before = files(tmp_path)
    assert main(['rehash', str(tmp_path)]) == 0
    assert 'Rehashed 4 completed stages' in capsys.readouterr().out
    after = files(tmp_path)
    assert set(before) == set(after)
    completed = {f'state/{s}.json': s for s in STAGES if read_json(tmp_path/f'state/{s}.json')['status'] == 'completed'}
    for relative, content in before.items():
        if relative in completed:
            import json
            expected = json.loads(content[0])
            expected['config_hash'] = stage_hash(load(tmp_path), completed[relative])
            assert read_json(tmp_path/relative) == expected
        else:
            assert after[relative] == content
    assert rehash(tmp_path, load(tmp_path)) == 0
    assert files(tmp_path) == after


def test_rehash_refuses_an_active_run_without_modifying_state(tmp_path):
    (tmp_path/'experiment.yaml').write_text('name: migration\nproject: {repo: /unused}\nanchor: t\n')
    write_json(tmp_path/'state/prepare.json', {'status': 'completed', 'config_hash': 'old'})
    with run_lock(tmp_path):
        before = files(tmp_path)
        with pytest.raises(PipelineError, match='already alive'):
            rehash(tmp_path, load(tmp_path))
        assert files(tmp_path) == before
