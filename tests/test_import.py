import json
from pathlib import Path
import yaml

from test_survival.config import load,stage_hash
from test_survival.importer import import_experiment
from test_survival.io import read_json


def test_legacy_retry_and_round_manifest_synthesis(tmp_path):
    src=tmp_path/'reference';exp=tmp_path/'replay';exp.mkdir()
    config={'name':'test','project':{'repo':'/project'},'anchor':'v1','generators':[{'name':'o','method':'ouedraogo','impl':'native','rounds':{'max':2}}]}
    (exp/'experiment.yaml').write_text(yaml.safe_dump(config))
    for name in ['round2','round1-retry','round1']:
        p=src/'generated/o'/name;p.mkdir(parents=True)
        (p/'funnel.json').write_text('{"compiled":0}')
        (p/'source.java').write_text('source')
    import_experiment(exp,src,'generated')
    manifest=read_json(exp/'generated/o/rounds.json')
    assert manifest['source']=='imported'
    assert [r['directory'] for r in manifest['rounds']]==['round1','round1-retry','round2']
    assert [r['round'] for r in manifest['rounds']]==[1,2,3]
    assert [r['archived_retry'] for r in manifest['rounds']]==[0,1,0]
    assert not (src/'generated/o/rounds.json').exists()
    state=read_json(exp/'state/generate.json')
    assert state['config_hash']==stage_hash(load(exp),'generate')
    assert state['status']=='completed'
    assert len(read_json(exp/'state/import.json')['generated']['files'])==6


def test_import_checks_ktester_post_process_before_copying(tmp_path):
    import pytest
    from test_survival.io import write_json
    from test_survival.pipeline import PipelineError
    src, dest = tmp_path/'source', tmp_path/'destination'
    dest.mkdir()
    (dest/'experiment.yaml').write_text(yaml.safe_dump(dict(name='fixture', project=dict(repo='/project'),
        anchor='v1', generators=[dict(name='k', method='ktester', tool_path='/unused')])))
    archive = src/'generated/k/round1'
    write_json(archive/'run.json', dict(status='completed'))
    write_json(archive/'funnel.json', dict(compiled=0))
    with pytest.raises(PipelineError, match='unverified KTester'):
        import_experiment(dest, src, 'generated')
    assert not (dest/'generated').exists()
