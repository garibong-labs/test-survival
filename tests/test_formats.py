import csv
import json
from test_survival import formats
from test_survival.io import write_csv,write_json


def test_csv_and_json_contracts(tmp_path):
    path=tmp_path/'commits.csv';row=dict(zip(formats.COMMITS,['1','sha','2023-12-15T14:09:28-05:00','subject, with comma','yes','no']))
    write_csv(path,[row],formats.COMMITS)
    assert path.read_bytes().splitlines()[0]==b'index,sha,date,subject,changes-src-main,changes-pom'
    assert list(csv.DictReader(path.open()))==[row]
    record={'status':'completed','config_hash':'hash','started':'start','finished':'finish','notes':'done'}
    write_json(tmp_path/'state.json',record)
    assert json.loads((tmp_path/'state.json').read_text())==record
    assert not list(tmp_path.glob('*.tmp'))


def test_group_column_order_matches_archived_schema():
    groups=['dev','ktester','ouedraogo','ouedraogo-adj','ouedraogo-adj2']
    fields=formats.changed(groups)
    assert fields[:9]==['index','sha','date','subject','all_executable','all_dev_covered','all_dev_percent','all_ktester_covered','all_ktester_percent']
    assert fields[-4:]==['ouedraogo-adj_lost','ouedraogo-adj_pre_image_covered','ouedraogo-adj2_lost','ouedraogo-adj2_pre_image_covered']
    assert formats.survival(['dev','ktester'])==['index','sha','date','subject','evaluated','dev_alive','dev_lost','ktester_alive','ktester_lost']
    assert formats.LLM==['file','class','method','kind','body_hash','round','technique']
