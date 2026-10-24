import json
from pathlib import Path
import yaml

from test_survival.config import load,stage_hash
from test_survival.importer import import_experiment
from test_survival.io import read_json


def test_legacy_retry_and_round_manifest_synthesis(tmp_path):
    src=tmp_path/'reference';exp=tmp_path/'replay';exp.mkdir()
    config={'name':'test','project':{'repo':'/project'},'anchor':'v1','generators':[{'name':'o','method':'ouedraogo','impl':'native'}]}
    (exp/'experiment.yaml').write_text(yaml.safe_dump(config))
    for name in ['round2','round1-retry','round1']:
        p=src/'generated/o'/name;p.mkdir(parents=True)
        (p/'funnel.json').write_text('{"compiled":0}')
        (p/'source.java').write_text('source')
    import_experiment(exp,src,'generated')
    manifest=read_json(exp/'generated/o/rounds.json')
    assert manifest['source']=='imported'
    assert [r['directory'] for r in manifest['rounds']]==['round1','round1-retry','round2']
    assert not (src/'generated/o/rounds.json').exists()
    state=read_json(exp/'state/generate.json')
    assert state['config_hash']==stage_hash(load(exp),'generate')
    assert state['status']=='completed'
    assert len(read_json(exp/'state/import.json')['generated']['files'])==6
