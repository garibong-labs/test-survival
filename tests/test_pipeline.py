from copy import deepcopy
import os

import pytest

from test_survival.config import STAGES, validate
from test_survival.io import read_json
from test_survival.pipeline import PipelineError, run, run_lock, status
from test_survival.cli import main


@pytest.fixture
def config():
    return validate({'name': 'test', 'project': {'repo': '/project'}, 'anchor': 'v1'})


def fake_stages(seen):
    def handler(stage):
        def invoke(exp, config):
            seen.append(stage)
            return f'{stage} evidence ready'
        return invoke
    return {s: handler(s) for s in STAGES}


def test_resume_and_changed_hash(tmp_path, config):
    seen = []
    handlers = fake_stages(seen)
    assert run(tmp_path, config, until='scope', handlers=handlers) == ['prepare', 'scope']
    assert run(tmp_path, config, handlers=handlers) == list(STAGES[2:])
    assert run(tmp_path, config, handlers=handlers) == []
    modified = deepcopy(config)
    modified['screen']['matching'] = 'subsample-to-dev'
    assert run(tmp_path, modified, handlers=handlers) == list(STAGES[3:])
    assert read_json(tmp_path / 'state/report.json')['status'] == 'completed'
    assert 'report     completed' in status(tmp_path, modified)


def test_stage_prerequisites_force_invalidates_later(tmp_path, config):
    handlers = fake_stages([])
    with pytest.raises(PipelineError, match='requires'):
        run(tmp_path, config, stage='screen', handlers=handlers)
    run(tmp_path, config, handlers=handlers)
    assert run(tmp_path, config, stage='scope', force=True, handlers=handlers) == ['scope']
    assert read_json(tmp_path / 'state/report.json')['status'] == 'pending'
    assert run(tmp_path, config, handlers=handlers) == list(STAGES[2:])


def test_failed_stage_resumes_and_preserves_evidence(tmp_path, config):
    seen = []
    handlers = fake_stages(seen)
    def fail(exp, config):
        (exp / 'evidence.txt').write_text('partial')
        raise RuntimeError('failure')
    handlers['scope'] = fail
    with pytest.raises(RuntimeError):
        run(tmp_path, config, handlers=handlers)
    assert read_json(tmp_path / 'state/scope.json')['status'] == 'failed'
    assert (tmp_path / 'evidence.txt').read_text() == 'partial'
    assert run(tmp_path, config, until='scope', handlers=fake_stages(seen)) == ['scope']


def test_live_lock_refuses_second_run(tmp_path, config):
    with run_lock(tmp_path):
        with pytest.raises(PipelineError, match='already alive'):
            run(tmp_path, config, handlers=fake_stages([]))
    run(tmp_path, config, until='prepare', handlers=fake_stages([]))
    assert read_json(tmp_path / 'state/run.json')['pid'] == os.getpid()


def test_init_and_status(tmp_path, capsys):
    assert main(['init', str(tmp_path)]) == 0
    original = (tmp_path / 'experiment.yaml').read_text()
    assert '#' in original
    assert main(['init', str(tmp_path)]) == 2
    assert (tmp_path / 'experiment.yaml').read_text() == original
    assert main(['status', str(tmp_path)]) == 0
    assert 'prepare    pending' in capsys.readouterr().out
