import pytest
from test_survival.config import validate
from test_survival.rules.scope import select_scope


@pytest.mark.parametrize('rule',['change-ranked','dedicated-test'])
def test_skip_popular_classes_continue_and_count_union(rule):
    rows=[dict({'class':c},m=10,c=10-i,has_test='yes') for i,c in enumerate('ABCD')]
    covered={'A':set(range(401)), 'B':set(range(200)), 'C':set(range(199,401)), 'D':set(range(200,400))}
    config=validate(dict(name='x',anchor='t',project=dict(repo='/r'),scope=dict(rule=rule,target_methods=[20,80])))['scope']
    skipped=[]
    chosen,eligible=select_scope(rows,config,lambda rows:len(set().union(*(covered[r['class']] for r in rows))),skipped)
    assert [r['class'] for r in chosen] == ['B','D']
    assert [(r['class'],r['would_be_dev_scope']) for r in skipped] == [('A',401),('C',401)]
    assert eligible == 4
    assert all(r['reason']=='max-dev-scope' for r in skipped)


def test_existing_stop_conditions_and_unscoped_rules_unchanged():
    rows=[dict({'class':c},m=30,c=10-i,has_test='yes') for i,c in enumerate('ABC')]
    raw=dict(name='x',anchor='t',project=dict(repo='/r'))
    config=validate(raw)['scope']
    assert select_scope(rows,config) == select_scope(rows,config,lambda _:399)
    for rule in ('explicit','all-eligible'):
        config=validate({**raw,'scope':dict(rule=rule,classes=['A'])})['scope']
        assert select_scope(rows,config,lambda _:10000) == select_scope(rows,config)


def test_scope_tuning_reuses_full_candidate_measurement_but_source_changes_do_not(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from test_survival.stages.scope import candidate_coverage
    from test_survival.io import write_json,read_json
    config=validate(dict(name='x',anchor='t',project=dict(repo='/r'),dev=dict(denominator='coverage-scoped')))
    source=tmp_path/'dev/src-test-t/java/p/Test.java';source.parent.mkdir(parents=True);source.write_text('test source')
    write_json(tmp_path/'results/anchor.json',dict(sha='t'))
    calls=[]
    def worker(job):
        calls.append(job)
        return dict(coverage_status='complete',methods=[dict(m,state='pass',lines=[['src/main/java/A.java',1]]) for m in job['methods']],runs=[dict(seconds=2)])
    adapter=SimpleNamespace(provenance=lambda:dict(helper='hash'),setup_runner=lambda:None,
        worker=worker,runner=SimpleNamespace(session=lambda _:nullcontext()))
    baseline=[dict({'class':'p.Test'},method='test',kind='plain',stable='yes')]
    first=candidate_coverage(tmp_path,config,adapter,baseline)
    config['scope']['max_dev_scope']=1
    assert candidate_coverage(tmp_path,config,adapter,baseline)==first
    assert len(calls)==1
    assert read_json(tmp_path/'results/dev-candidate-coverage-t-timing.json')['reused']
    source.write_text('changed test source')
    candidate_coverage(tmp_path,config,adapter,baseline)
    assert len(calls)==2


def test_scope_stage_records_skips_and_selected_denominator(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from test_survival.stages import scope
    from test_survival.io import write_json,write_csv,read_json,read_csv
    config=validate(dict(name='x',anchor='t',project=dict(repo='/r'),dev=dict(denominator='coverage-scoped'),scope=dict(target_methods=[3,80])))
    methods=[dict({'class':cls},method=f'm{i}',signature=f'm{i}()',line=i+1,eligible=True)
             for cls in ('p.Popular','p.Focused') for i in range(3)]
    write_json(tmp_path/'results/source-inventory.json',dict(main=dict(classes={cls:dict(path=f'src/main/java/{cls}.java') for cls in ('p.Popular','p.Focused')},methods=methods),test=dict(classes={})))
    write_json(tmp_path/'results/commit-paths.json',{'1':['src/main/java/p.Popular.java'],'2':['src/main/java/p.Popular.java','src/main/java/p.Focused.java']})
    baseline=[dict({'class':'p.Tests'},method=f't{i}',kind='plain',stable='yes',
                   declaring_class='p.Base',declaring_file='dev/src-test-t/java/p/Base.java') for i in range(401)]
    write_csv(tmp_path/'dev/dev-methods-all.csv',baseline,['class','method','kind','stable','declaring_class','declaring_file'])
    def execute(command,cwd,log,timeout):
        log.parent.mkdir(parents=True,exist_ok=True)
        log.write_text('class\tmethod\taccessor\np.Popular\tm0\tfalse\np.Focused\tm0\tfalse\n')
        return dict(exit_code=0)
    adapter=SimpleNamespace(ensure_anchor=lambda:None,runner=SimpleNamespace(execute=execute),helpers=tmp_path,folder=lambda _:tmp_path)
    monkeypatch.setattr(scope,'JavaMavenAdapter',lambda *a:adapter)
    record=dict(measurement_seconds=2,methods=[dict(m,state='pass',lines=[['src/main/java/p.Popular.java',1]]+([['src/main/java/p.Focused.java',1]] if i==0 else [])) for i,m in enumerate(baseline)])
    monkeypatch.setattr(scope,'candidate_coverage',lambda *a:record)
    scope.run(tmp_path,config)
    selection=read_json(tmp_path/'results/scope-selection.json')
    assert selection['developer_denominator']==1
    assert selection['skipped_classes'][0]['class']=='p.Popular'
    assert selection['skipped_classes'][0]['would_be_dev_scope']==401
    assert 'would-be Dev(scope)=401' in (tmp_path/'population/scope.md').read_text()
    assert [r['method'] for r in read_csv(tmp_path/'dev/dev-denominator.csv')]==['t0']
    assert read_csv(tmp_path/'dev/dev-denominator.csv')[0]['declaring_class']=='p.Base'
    assert all(r['declaring_file'].endswith('/p/Base.java') for r in read_csv(tmp_path/'dev/dev-excluded.csv'))
    # A completed group's import invalidates scope, but must retain the same
    # validated instrumentation boundary without remeasuring the imported pool.
    from test_survival import dev_results
    from test_survival.pipeline import PipelineError
    saved=read_json(tmp_path/'population/scope.json')
    write_json(tmp_path/'state/import.json',{'dev-results':dict(
        compatibility=dict(resolved_scope=dev_results.resolved_scope(saved['classes'])),
        scope_selection=dict(config=config['scope'],population=saved,selection=selection))})
    config['survival']['execution']='batch'
    monkeypatch.setattr(dev_results,'imported_groups',lambda *a:{'dev':dict(frozen=read_csv(tmp_path/'dev/dev-denominator.csv'))})
    monkeypatch.setattr(scope,'candidate_coverage',lambda *a:pytest.fail('Imported candidates must not be remeasured'))
    scope.run(tmp_path,config)
    assert read_json(tmp_path/'population/scope.json')['classes']==saved['classes']
    assert read_json(tmp_path/'results/scope-selection.json')['skipped_classes']==selection['skipped_classes']
    config['scope']={**config['scope'],'rule':'explicit','classes':['p.Popular']}
    with pytest.raises(PipelineError,match='resolved coverage scope differs'):
        scope.run(tmp_path,config)


def test_source_excluded_overloads_never_reach_candidate_measurement(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from test_survival.stages.scope import candidate_coverage
    from test_survival.rules.dev_denominator import coverage_scoped
    from test_survival.io import write_json
    config=validate(dict(name='x',anchor='t',project=dict(repo='/r'),
        dev=dict(discovery='source',unmapped='exclude',denominator='coverage-scoped')))
    source=tmp_path/'dev/src-test-t/java/p/Tests.java';source.parent.mkdir(parents=True);source.write_text('fixture')
    write_json(tmp_path/'results/anchor.json',dict(sha='t'))
    baseline=[dict({'class':'p.Tests'},method=n,kind='plain',stable=stable)
              for n,stable in [('works','yes'),('overloaded','no'),('overloaded','no'),('failing','no')]]
    measured=[]
    def worker(job):
        measured.extend(job['methods'])
        return dict(coverage_status='complete',methods=[dict(t,state='pass',lines=[['src/main/java/P.java',1]]) for t in job['methods']],runs=[dict(seconds=1)])
    adapter=SimpleNamespace(setup_runner=lambda:None,provenance=lambda:{},worker=worker,
                            runner=SimpleNamespace(session=lambda _:nullcontext()))
    result=candidate_coverage(tmp_path,config,adapter,baseline)
    observations=[dict(t,status=t['state'],scope_lines_covered=len(t['lines'])) for t in result['methods']]
    kept,_=coverage_scoped(baseline,observations)
    assert [m['method'] for m in measured]==['works']
    assert [m['method'] for m in kept]==['works']


def test_build_coverage_still_measures_unique_unstable_candidates(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from test_survival.stages.scope import candidate_coverage
    from test_survival.io import write_json
    config=validate(dict(name='x',anchor='t',project=dict(repo='/r'),dev=dict(discovery='build')))
    source=tmp_path/'dev/src-test-t/java/p/Tests.java';source.parent.mkdir(parents=True);source.write_text('fixture')
    write_json(tmp_path/'results/anchor.json',dict(sha='t'))
    baseline=[dict({'class':'p.Tests'},method='unstable',kind='plain',stable='no')]
    def worker(job):
        assert [m['method'] for m in job['methods']]==['unstable']
        return dict(coverage_status='complete',methods=[dict(t,state='fail',lines=[]) for t in job['methods']],runs=[dict(seconds=1)])
    adapter=SimpleNamespace(setup_runner=lambda:None,provenance=lambda:{},worker=worker,
                            runner=SimpleNamespace(session=lambda _:nullcontext()))
    assert len(candidate_coverage(tmp_path,config,adapter,baseline)['methods'])==1
