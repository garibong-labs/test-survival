import importlib.util
from pathlib import Path

from test_survival.io import read_csv, read_json, write_json


def test_per_index_wall_components_deduplicate_shared_snapshot_compiles(tmp_path):
    spec=importlib.util.spec_from_file_location('affected_performance',Path(__file__).parents[1]/'scripts/affected_selection_performance.py')
    performance=importlib.util.module_from_spec(spec);spec.loader.exec_module(performance)
    compile_attempt=dict(log='compile.txt',seconds=2)
    for mode in ('all','affected'):
        (tmp_path/f'{mode}-prepare-scope.log').write_text(
            '2026-01-01T00:00:00+00:00 prepare: started\n'
            '2026-01-01T00:00:02+00:00 prepare: anchor build ok\n'
            '2026-01-01T00:00:03+00:00 prepare: baseline selected\n'
            '2026-01-01T00:00:05+00:00 prepare: build 1/1 ok\n')
        (tmp_path/f'{mode}-survive.log').write_text(
            '2026-01-01T00:00:10+00:00 survive: started\n'
            '2026-01-01T00:00:20+00:00 survive: 1 batch recorded\n')
        write_json(tmp_path/f'{mode}-timing.json',dict(steps=[dict(stage='control',seconds=12)]))
        write_json(tmp_path/f'reference-{mode}.json',dict(seconds=10))
        for index in (0,1):
            write_json(tmp_path/f'repo/experiments/{mode}/results/baseline-batches/{index}.json',
                       dict(sha=str(index),compile=[dict(attempts=[compile_attempt])]*3,runs=[dict(seconds=3)],
                            selection_counts=dict(executed=1,carried=int(mode=='affected' and index==1))))
    performance.timing(tmp_path)
    rows=read_csv(tmp_path/'all-per-index-performance.csv')
    assert float(rows[1]['production_build_seconds'])==2
    assert float(rows[1]['test_compile_seconds'])==2  # shared by three manifest groups
    assert float(rows[1]['test_jvm_seconds'])==3
    assert float(rows[1]['other_seconds'])==5
    assert float(rows[1]['total_seconds'])==12
    assert read_csv(tmp_path/'affected-per-index-performance.csv')[1]['ratio']==''
    assert read_json(tmp_path/'performance-summary.json')['all']['max_ratio']==1.2


def test_policy_timing_keeps_boundary_phases_and_ignores_late_skipped_records(tmp_path):
    import os
    from test_survival.io import write_csv
    spec=importlib.util.spec_from_file_location('affected_performance',Path(__file__).parents[1]/'scripts/affected_selection_performance.py')
    performance=importlib.util.module_from_spec(spec);spec.loader.exec_module(performance)
    start='2026-01-01T00:00:00+00:00'
    for mode in ('all','affected'):
        exp=tmp_path/f'repo/experiments/{mode}-policies'
        write_json(exp/'state/policy-survive.json',dict(started=start))
        epoch=performance.datetime.fromisoformat(start).timestamp()
        for index,seconds,offset in ((0,2,1),(1,1,3),(3,1,-60)):
            log=exp/f'results/builds/logs/{index}.txt';log.parent.mkdir(parents=True,exist_ok=True)
            log.write_text('build evidence')
            os.utime(log,(epoch+offset,epoch+offset))
            write_json(exp/f'results/builds/{index}.json',dict(attempts=[dict(seconds=seconds,log=str(log.relative_to(exp)))]))
        for index,end in ((1,14),(3,20)):
            write_json(exp/f'results/policy/batches/{index}.json',dict(index=index,methods=[{}],
                compile=[dict(attempts=[dict(log='shared',seconds=2)])]*2,runs=[dict(seconds=1)]))
            decision=exp/f'results/policy/reset/decisions/dev/2/{index}.json'
            write_json(decision,dict(index=index))
            stamp=performance.datetime.fromisoformat(start).timestamp()+end
            os.utime(decision,(stamp,stamp))
        # Skipped-index projections can be written after all actual work.
        decision=exp/'results/policy/reset/decisions/dev/2/2.json'
        write_json(decision,dict(index=2));os.utime(decision,(epoch+30,epoch+30))
        write_csv(tmp_path/f'repo/experiments/{mode}/results/survival.csv',
                  [dict(index=i,sha=str(i),evaluated='no' if i==2 else 'yes') for i in (1,2,3)],['index','sha','evaluated'])
    (tmp_path/'all-policies-run.log').write_text(
        '2026-01-01T00:00:10+00:00 policy-run-abc-1: completed\n'
        '2026-01-01T00:00:14+00:00 policy-run-abc-1: completed\n'
        '2026-01-01T00:00:20+00:00 policy-run-abc-3: completed\n'
        '2026-01-01T00:00:30+00:00 policy-run-abc-2: completed\n')
    performance.policy_timing(tmp_path)
    for mode in ('all','affected'):
        rows=read_csv(tmp_path/f'{mode}-policies-per-index-performance.csv')
        assert [r['index'] for r in rows]==['1','3']
        assert [float(r['total_seconds']) for r in rows]==[14,6]
        assert [float(r['production_build_seconds']) for r in rows]==[3,0]
        assert [float(r['other_seconds']) for r in rows]==[8,3]
