from test_survival.cli import main
from test_survival.io import write_csv, write_json


def test_completed_pre_framework_results_load_without_configuration(tmp_path,capsys):
    write_json(tmp_path/'results/final-summary.json',dict(status='completed',denominators={'dev':4}))
    write_csv(tmp_path/'results/survival.csv',[dict(index=0,dev_alive=4),dict(index=1,dev_alive=2)],['index','dev_alive'])
    before={p.relative_to(tmp_path):(p.stat().st_size,p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
    assert main(['status',str(tmp_path)]) == 0
    assert 'experiment.yaml is unavailable' in capsys.readouterr().out
    assert main(['show',str(tmp_path),'report']) == 0
    text=capsys.readouterr().out
    assert 'report: completed' in text and 'Alive at index 1' in text
    after={p.relative_to(tmp_path):(p.stat().st_size,p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
    assert before==after
    assert main(['run',str(tmp_path),'--until','scope'])==2


def test_missing_configuration_without_completed_results_is_not_silently_accepted(tmp_path):
    assert main(['status',str(tmp_path)])==2


def test_saved_policy_status_does_not_require_missing_baseline_configuration(tmp_path,monkeypatch):
    from test_survival.policy import pipeline
    write_json(tmp_path/'state/policy-report.json',dict(status='completed'))
    def missing(*args):raise FileNotFoundError('historical baseline config moved')
    monkeypatch.setattr(pipeline,'Baseline',missing)
    assert 'policy-report.json: completed' in pipeline.status(tmp_path,{})
