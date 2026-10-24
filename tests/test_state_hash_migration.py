import importlib.util
import json
from pathlib import Path

import pytest

from test_survival.config import load, stage_hash

spec=importlib.util.spec_from_file_location('migrate_state_hashes',Path(__file__).parents[1]/'scripts/migrate_state_hashes.py')
migration=importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def experiment(tmp_path):
    (tmp_path/'experiment.yaml').write_text('name: example\nproject: {repo: /project}\nanchor: v1\n')
    (tmp_path/'state').mkdir()
    return tmp_path


def test_migration_changes_only_hash_and_is_idempotent(tmp_path):
    exp=experiment(tmp_path);path=exp/'state/prepare.json'
    original='{ "status": "completed", "config_hash" : "old", "notes": "retained", "extra": {"x": [1, 2]} }\n'
    path.write_text(original)
    evidence=tmp_path/'evidence.json'
    records=migration.migrate(exp,['prepare'],evidence)
    digest=stage_hash(load(exp),'prepare')
    assert path.read_text()==original.replace('"old"','"'+digest+'"')
    assert records==[{'stage':'prepare','before':'old','after':digest,'changed':True}]
    timestamp=path.stat().st_mtime_ns
    assert not migration.migrate(exp,['prepare'],evidence)[0]['changed']
    assert path.stat().st_mtime_ns==timestamp
    assert len(json.loads(evidence.read_text())['migrations'])==2


def test_running_stage_prevents_all_migration_writes(tmp_path):
    exp=experiment(tmp_path)
    first=exp/'state/prepare.json';first.write_text('{"status":"completed","config_hash":"old"}')
    (exp/'state/scope.json').write_text('{"status":"running","config_hash":"old"}')
    before=first.read_bytes()
    with pytest.raises(ValueError,match='Only completed'):
        migration.migrate(exp,['prepare','scope'],tmp_path/'evidence.json')
    assert first.read_bytes()==before
    assert not (tmp_path/'evidence.json').exists()
