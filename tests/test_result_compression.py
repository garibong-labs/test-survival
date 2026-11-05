import gzip
import hashlib
import json
from pathlib import Path

import pytest

from test_survival import io
from test_survival.dev_results import file_hash
from test_survival.adapters import java_worker
from test_dev_results import experiments


def test_logical_reader_hashes_and_duplicates(tmp_path):
    path = tmp_path/'results/raw/1-dev.json'
    value = {'methods':[{'id':'p.T#café','state':'pass'}]}
    io.write_json(path, value)
    original = path.read_bytes()
    assert file_hash(path) == hashlib.sha256(original).hexdigest()
    zipped = io.compress_file(path)
    assert not path.exists() and io.result_exists(path)
    assert io.read_json(path) == value
    assert io.read_bytes(path) == original
    assert file_hash(path) == file_hash(zipped) == hashlib.sha256(original).hexdigest()
    assert list(io.logical_glob(path.parent, '*.json')) == [path]
    path.write_bytes(original)
    assert io.read_json(path) == value
    path.write_text('{}')
    with pytest.raises(ValueError, match='Conflicting'):
        io.read_json(path)
    with pytest.raises(ValueError, match='Conflicting'):
        file_hash(path)


def test_atomic_writers_preserve_logical_bytes_and_summary_layout(tmp_path):
    for relative in ('results/raw/a.json', 'results/raw-derived/a.json', 'results/raw-coverage/a.json',
                     'results/dev-method-coverage-t/Test#method.json',
                     'results/policy/pools/dev/1/results/raw/a.json'):
        path = tmp_path/relative
        java_worker.write_json(path, {'value':'café'})
        assert not path.exists()
        assert io.read_bytes(path) == (json.dumps({'value':'café'},indent=2)+'\n').encode()
    for relative in ('generated/llm/round1/validation/0.json','state/cover.json','results/final-summary.json',
                     'results/policy/runs/1.json'):
        path=tmp_path/relative
        java_worker.write_json(path, {'value':1})
        assert path.is_file() and not path.with_name(path.name+'.gz').exists()
    csv=tmp_path/'results/coverage/0-dev-scope-lines.csv'
    rows=[{'file':'p/A.java','line':'2','covered':'yes'}]
    io.write_csv(csv, rows, list(rows[0]), compress=True)
    assert io.read_csv(csv) == rows
    assert io.read_bytes(csv) == b'file,line,covered\np/A.java,2,yes\n'
    xml=csv.with_name('0-dev-scope.xml');xml.write_text('<report/>')
    io.compress_file(xml)
    assert io.read_bytes(xml) == b'<report/>'


def test_corrupt_gzip_is_not_missing_and_sources_hash_physical_bytes(tmp_path):
    path=tmp_path/'results/raw/1-dev.json';path.parent.mkdir(parents=True)
    path.with_name(path.name+'.gz').write_bytes(b'bad')
    with pytest.raises(gzip.BadGzipFile):io.read_json(path,{})
    source=tmp_path/'dev/src-test-t/resources/data.gz';source.parent.mkdir(parents=True)
    source.write_bytes(gzip.compress(b'fixture'))
    assert file_hash(source) == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize('compressed', [False, True])
def test_import_report_progress_and_coverage_accept_both_layouts(experiments, compressed):
    from test_survival.config import load
    from test_survival.importer import import_experiment
    from test_survival.report.consistency import build
    from test_survival.progress import snapshot
    from test_survival.stages import control, survive, cover
    from test_survival.dev_results import imported_groups
    source,target,calls=experiments
    if compressed:
        for path in list(source.rglob('*')):
            if path.is_file() and io.compressed_result(path) and path.suffix != '.gz':
                io.compress_file(path)
    import_experiment(target,source,'dev-results')
    config=load(target)
    for stage in (control,survive,cover):stage.run(target,config)
    summary=build(target,config)
    assert summary['final_survivors']['dev-all'] == 1
    assert summary['final_survivors']['dev-scope'] == 0
    assert summary['final_verification']['raw_count_matches']
    assert snapshot(target,'control',config)['done'] >= 3
    assert imported_groups(target,config,'cover')
    if compressed:
        assert (target/'results/coverage/0-dev-all.xml.gz').is_file()
        assert not (target/'results/coverage/0-dev-all.xml').exists()


def test_progress_stats_compressed_paths(experiments):
    from test_survival.config import load
    from test_survival.progress import description, snapshot
    from datetime import datetime, timezone
    source,_,_=experiments
    for path in list(source.rglob('*')):
        if path.is_file() and io.compressed_result(path) and path.suffix != '.gz':io.compress_file(path)
    # Imported provenance makes these archived records current for this fixture.
    from test_survival.dev_results import import_results
    # Test the physical-stat boundary independently of fingerprint calculations.
    from unittest.mock import patch
    path=source/'results/raw/1-dev-all.json'
    io.write_json(source/'state/survive.json',dict(status='running',started='2026-01-01T00:00:00+00:00'))
    with patch('test_survival.progress.items',return_value=([[path]],1,'commits')):
        assert '1/1 commits' in description(source,'survive',load(source)).render()
    with patch('test_survival.progress.panel_items',return_value=([[path]],1,'commits',{})):
        assert snapshot(source,'survive',load(source))['done']==1


def test_coverage_coordinator_rejects_bad_universe_before_release(tmp_path):
    from test_survival.stages.cover import validate_index
    fields=['file','line','covered']
    row=dict(file='A.java',line=1,covered='yes')
    for group in ('dev','dev-scope'):
        io.write_csv(tmp_path/f'results/coverage/2-{group}-lines.csv',[row],fields,compress=True)
    validate_index(tmp_path,2,['dev','dev-scope'])
    io.write_csv(tmp_path/'results/coverage/2-dev-lines.csv',[row,row],fields,compress=True)
    with pytest.raises(RuntimeError,match='Duplicate'):validate_index(tmp_path,2,['dev','dev-scope'])
    io.write_csv(tmp_path/'results/coverage/2-dev-lines.csv',[dict(row,line=2)],fields,compress=True)
    with pytest.raises(RuntimeError,match='universes differ'):validate_index(tmp_path,2,['dev','dev-scope'])


def test_container_result_permissions_preserve_host_readability(tmp_path):
    import stat
    path=tmp_path/'results/raw/1-dev.json'
    java_worker.write_json(path, {'value':1})
    assert stat.S_IMODE(io.resolve_path(path).stat().st_mode)==0o644
    csv=tmp_path/'results/coverage/1-dev-lines.csv'
    io.write_csv(csv,[],['file','line','covered'],compress=True,permissions=0o644)
    assert stat.S_IMODE(io.resolve_path(csv).stat().st_mode)==0o644
    xml=csv.with_name('1-dev.xml');xml.write_text('<report/>');xml.chmod(0o640)
    assert stat.S_IMODE(io.compress_file(xml).stat().st_mode)==0o640


def test_finished_main_evidence_remains_readable_without_hash_migration(tmp_path,monkeypatch):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.adapters.run_reuse import survival_provenance_matches,pre_footprint_provenance
    from test_survival.config import validate
    from test_survival.progress import snapshot
    previous=io.read_json(Path(__file__).parent/'fixtures/footprint-main-provenance.json')
    config=validate(dict(name='old',anchor='t',project={'repo':str(tmp_path)},
                         generators=[dict(name='llm',method='ouedraogo',impl='native')]))
    adapter=JavaMavenAdapter(tmp_path,config);current=adapter.provenance()
    assert previous != pre_footprint_provenance(current)
    assert not survival_provenance_matches(previous,current)
    for key in previous:
        assert not survival_provenance_matches({**previous,key:'unknown'},current)
    source=tmp_path/'generated/llm/round1/sources/T.java';source.parent.mkdir(parents=True);source.write_text('class T {}')
    record=dict(file=str(source.relative_to(tmp_path)),input_sha256=adapter.screen_fingerprint(source,3,provenance=previous),methods=[])
    io.write_json(tmp_path/'generated/llm/round1/validation/0.json',record)
    io.write_json(tmp_path/'state/screen.json',dict(status='completed'))
    io.write_json(tmp_path/'state/report.json',dict(status='completed'))
    before={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.setattr(JavaMavenAdapter,'worker',lambda *a:pytest.fail('status must not execute'))
    assert io.read_json(tmp_path/'generated/llm/round1/validation/0.json')==record
    assert snapshot(tmp_path,'screen',config)['done']==1
    assert before=={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
