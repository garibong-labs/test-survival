import pytest
import yaml

from test_survival.cli import main
from test_survival.config import STAGES, load, stage_hash, legacy_stage_hash
from test_survival.io import read_json, write_json
from test_survival.pipeline import PipelineError, rehash, run_lock


def files(exp):
    return {str(p.relative_to(exp)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in exp.rglob('*') if p.is_file()}


def test_rehash_changes_only_completed_stage_config_hashes(tmp_path, capsys):
    (tmp_path/'experiment.yaml').write_text(yaml.safe_dump(dict(name='migration', project={'repo': '/unused'}, anchor='t', runner={'workers': {'build': 2}})))
    config = load(tmp_path)
    for i, stage in enumerate(STAGES):
        write_json(tmp_path/f'state/{stage}.json', dict(status=['completed', 'failed', 'completed', 'pending'][i%4],
                   config_hash=legacy_stage_hash(config, stage), started='start', finished='finish', notes='retained', group_hashes={'k': 'old-group'}))
    write_json(tmp_path/'state/run.json', {'status': 'completed', 'config_hash': 'unchanged'})
    write_json(tmp_path/'state/import.json', {'source': 'unchanged'})
    write_json(tmp_path/'results/evidence.json', {'config_hash': 'unchanged'})
    write_json(tmp_path/'generated/k/rounds.json', {'config_hash': 'unchanged'})
    (tmp_path/'state/log.txt').write_text('unchanged log\n')
    (tmp_path/'state/run.lock').touch()
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


def test_rehash_does_not_bless_configuration_changes(tmp_path, capsys):
    from test_survival.config import validate
    config = validate(dict(name='changed', project={'repo': '/unused'}, anchor='t', runner={'workers': {'build': 2}}))
    for stage in STAGES:
        write_json(tmp_path/f'state/{stage}.json', {'status': 'completed', 'config_hash': legacy_stage_hash(config, stage)})
    (tmp_path/'state/run.lock').touch()
    before = files(tmp_path)
    config['anchor'] = 'new-anchor'
    assert rehash(tmp_path, config) == 0
    assert files(tmp_path) == before
    output = capsys.readouterr().out
    for stage in STAGES:
        assert f'{stage}: configuration changed after that stage completed' in output


def test_rehash_refuses_an_active_run_without_modifying_state(tmp_path):
    (tmp_path/'experiment.yaml').write_text('name: migration\nproject: {repo: /unused}\nanchor: t\n')
    write_json(tmp_path/'state/prepare.json', {'status': 'completed', 'config_hash': 'old'})
    with run_lock(tmp_path):
        before = files(tmp_path)
        with pytest.raises(PipelineError, match='already alive'):
            rehash(tmp_path, load(tmp_path))
        assert files(tmp_path) == before


def test_legacy_run_stops_before_invalidation_and_keeps_lock_inode(tmp_path):
    import fcntl
    import os
    from test_survival.config import legacy_stage_hash, validate
    from test_survival.pipeline import run
    config = validate(dict(name='legacy', project={'repo': '/unused'}, anchor='t', runner={'workers': {'build': 4}}))
    for stage in STAGES:
        write_json(tmp_path/f'state/{stage}.json', {'status': 'completed', 'config_hash': legacy_stage_hash(config, stage)})
    with run_lock(tmp_path):
        pass
    before = files(tmp_path)
    called = []
    with pytest.raises(PipelineError, match='survival rehash'):
        run(tmp_path, config, handlers={s: lambda *a: called.append(True) for s in STAGES})
    assert not called and files(tmp_path) == before
    fd = os.open(tmp_path/'state/run.lock', os.O_RDWR)
    try:
        inode = os.fstat(fd).st_ino
        rehash(tmp_path, config)
        assert (tmp_path/'state/run.lock').stat().st_ino == inode
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PipelineError, match='already alive'):
            with run_lock(tmp_path):
                pass
    finally:
        os.close(fd)


def test_rehash_without_existing_lock_changes_nothing(tmp_path):
    from test_survival.config import validate
    write_json(tmp_path/'state/prepare.json', {'status': 'completed', 'config_hash': 'old'})
    before = files(tmp_path)
    with pytest.raises(PipelineError, match='existing state/run.lock'):
        rehash(tmp_path, validate(dict(name='missing-lock', project={'repo': '/unused'}, anchor='t')))
    assert files(tmp_path) == before


def test_rehash_keeps_ledgers_but_group_rerun_reuses_other_generators(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from test_survival.config import legacy_stage_hash, validate
    from test_survival.io import write_csv
    from test_survival.pipeline import run
    from test_survival.stages import generate
    from test_survival.adapters.java_maven import JavaMavenAdapter
    config = validate(dict(name='legacy', project={'repo': '/unused'}, anchor='t', runner={'workers': {'build': 4}},
                           generators=[{'name': name, 'method': 'ouedraogo', 'impl': 'native'} for name in ('one', 'two')]))
    for stage in STAGES:
        write_json(tmp_path/f'state/{stage}.json', {'status': 'completed', 'config_hash': legacy_stage_hash(config, stage),
                   'group_hashes': {g['name']: legacy_stage_hash(config, 'generate', g['name']) for g in config['generators']}})
    for name in ('one', 'two'):
        write_json(tmp_path/f'generated/{name}/rounds.json', {'config_hash': legacy_stage_hash(config, 'generate', name), 'stopped': 'max-rounds'})
    write_csv(tmp_path/'dev/dev-denominator.csv', [], ['class', 'method', 'kind'])
    with run_lock(tmp_path):
        pass
    old_manifest = (tmp_path/'generated/two/rounds.json').read_bytes()
    old_groups = read_json(tmp_path/'state/generate.json')['group_hashes']
    rehash(tmp_path, config)
    assert read_json(tmp_path/'state/generate.json')['group_hashes'] == old_groups
    assert (tmp_path/'generated/two/rounds.json').read_bytes() == old_manifest
    config['runner']['workers']['build'] = 1
    calls = []
    class Generator:
        def __init__(self, exp, config, group):
            self.name = group['name']
        def generate(self, round_no):
            calls.append(self.name)
            return SimpleNamespace(calls=[])
    monkeypatch.setattr(generate, 'OuedraogoNative', Generator)
    monkeypatch.setattr(generate, 'screen_round', lambda *args: ([], {'compiled': 1}))
    monkeypatch.setattr(JavaMavenAdapter, 'setup_runner', lambda self: None)
    run(tmp_path, config, stage='generate', group='one', force=True)
    saved = read_json(tmp_path/'state/generate.json')
    assert saved['status'] == 'completed'
    assert saved['group_hashes']['two'] == stage_hash(config, 'generate', 'two')
    generate.run(tmp_path, config)
    assert calls == ['one']
    assert (tmp_path/'generated/two/rounds.json').read_bytes() == old_manifest
