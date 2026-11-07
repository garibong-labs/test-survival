from pathlib import Path
from types import SimpleNamespace

import pytest

from test_survival.adapters.build_discovery import report_methods, exclusion_reason, discover
from test_survival.config import validate, stage_hash, as_written
from test_survival.io import read_csv, read_json, write_json


def method(name, **extra):
    return dict({'class':'p.Tests'}, method=name, kind='plain', display='', **extra)


@pytest.mark.parametrize('name', ['plain', 'plain()', 'plain[0]', 'plain(int)[2]', 'plain[repeat 3]'])
def test_junit4_junit5_parameterized_and_repeated_mapping(tmp_path, name):
    (tmp_path/'TEST-other.xml').write_text(f'<testsuite><testcase classname="p.Tests" name="{name}"/></testsuite>')
    observed, unmapped, counts = report_methods(tmp_path, [method('plain')])
    assert dict(observed) == {('p.Tests','plain'):['pass']}
    assert unmapped == []
    assert counts == dict(run=1, failed=0, skipped=0)


def test_invocations_aggregate_skips_and_failures_and_reject_ambiguous_reports(tmp_path):
    (tmp_path/'TEST-p.Tests.xml').write_text('''<testsuite>
      <testcase name="repeat()[1]"><skipped/></testcase>
      <testcase name="repeat()[2]"/>
      <testcase name="repeat()[3]"><failure/></testcase>
      <testcase name="Custom label"/>
      <testcase name="unknown"/>
      <testcase name="overloaded(int)"/>
      <testcase name="3 / example"><system-out>[test-template:template(int)]</system-out></testcase>
    </testsuite>''')
    observed, unmapped, counts = report_methods(tmp_path, [method('repeat'),method('display',display_override=''),
        {**method('display'), 'display':'Custom label'}, method('overloaded'), method('overloaded'), method('template')])
    assert observed['p.Tests','repeat'] == ['skipped','pass','fail']
    assert observed['p.Tests','display'] == ['pass']
    assert observed['p.Tests','template'] == ['pass']
    assert [u['reason'] for u in unmapped] == ['no-source-method','ambiguous-source-method']
    assert counts == dict(run=6,failed=1,skipped=1)


@pytest.mark.parametrize('annotations,states,settings,expected', [
    ('@Disabled("later")',[],{},'disabled'),
    ('@Ignore',[],{},'disabled'),
    ('', ['skipped'],{},'skipped'),
    ('@Tag("slow")',[],{'excludedGroups':'slow,unstable,network'},'tag-or-group-exclusion'),
    ('@Category(p.Slow.class)',[],{'excludedGroups':'p.Slow'},'tag-or-group-exclusion'),
    ('@Tag("network")',[],{'groups':'fast'},'tag-or-group-exclusion'),
    ('',[],{'includes':['**/*Test.java']},'include-pattern'),
    ('',[],{'includes':['**/*.java'],'excludes':['**/Tests.java']},'exclude-pattern'),
    ('',[],{'includes':['**/*.java']},'not-reported-by-build'),
    ('',[],{'includes':['${patterns}']},'not-reported-by-build'),
])
def test_exclusion_diagnostics(annotations,states,settings,expected):
    assert exclusion_reason(method('x',annotations=annotations),states,settings) == expected


def test_build_candidates_use_non_skipped_invocations_and_audit_every_omission(tmp_path):
    folder = tmp_path/'work/checkouts/0/module';folder.mkdir(parents=True)
    (folder/'pom.xml').write_text('<project/>')
    write_json(tmp_path/'results/anchor.json',dict(sha='anchor'))
    calls = []
    def execute(command,cwd,log,timeout):
        calls.append((command,cwd))
        reports = folder/'target/surefire-reports';reports.mkdir(parents=True)
        (reports/'TEST-p.Tests.xml').write_text('''<testsuite><testcase name="x[1]"><skipped/></testcase>
           <testcase name="x[2]"/><testcase name="y"><failure/></testcase>
           <testcase name="z"><skipped/></testcase><testcase name="unmapped"/></testsuite>''')
        return dict(command=command,exit_code=0,seconds=2.5)
    config=validate(dict(name='fixture',anchor='anchor',project=dict(repo=str(tmp_path),module='module'),dev=dict(discovery='build')))
    adapter=SimpleNamespace(exp=tmp_path,config=config,folder=lambda _:folder,runner=SimpleNamespace(execute=execute))
    candidates=[method(n) for n in ['x','y','z','absent']]
    assert [m['method'] for m in discover(adapter,candidates)] == ['x','y']
    assert discover(adapter,candidates) == candidates[:2]
    config['runner']['workers']['build'] = 8
    assert discover(adapter,candidates+[method('newly-inventoried')]) == candidates[:2]
    assert calls == [(['mvn','-o','test','-Dmaven.test.failure.ignore=true','-pl','module','-am'], tmp_path/'work/checkouts/0')]
    assert [r['reason'] for r in read_csv(tmp_path/'dev/dev-not-run-by-build.csv')] == ['skipped','not-reported-by-build','not-reported-by-build']
    assert read_json(tmp_path/'results/dev-build-discovery.json')['counts'] == dict(run=3,failed=1,skipped=2,methods_run=2,excluded=3,unmapped=1)


def test_discovery_defaults_and_fingerprints_preserve_old_configs():
    raw=dict(name='old',anchor='v1',project=dict(repo='/repo'))
    config=validate(raw)
    assert config['dev']['discovery'] == 'source'
    assert config['scope']['max_dev_scope'] == 400
    assert as_written(config) == raw
    assert stage_hash(config,'prepare') != stage_hash(validate({**raw,'dev':{'discovery':'build'}}),'prepare')


def test_build_discovery_keeps_inherited_sources_excludes_tags_and_does_not_cap_candidates(tmp_path):
    from test_survival.adapters.inherited_tests import expand
    folder = tmp_path/'work/checkouts/0'; folder.mkdir(parents=True)
    (folder/'pom.xml').write_text('''<project><build><plugins><plugin>
        <artifactId>maven-surefire-plugin</artifactId><configuration>
        <excludedGroups>slow</excludedGroups></configuration></plugin></plugins></build></project>''')
    write_json(tmp_path/'results/anchor.json', dict(sha='t'))
    classes = {
        'p.Base': dict(kind='CLASS', package='p', modifiers='abstract', path='src/test/java/p/Base.java'),
        'p.Tests': dict(kind='CLASS', package='p', extends='Base', path='src/test/java/p/Tests.java'),
    }
    declared = [dict(method(f't{i}'), **{'class':'p.Base'}, signature=f't{i}()', modifiers='public') for i in range(450)]
    declared.append(dict(method('slow'), **{'class':'p.Base'}, signature='slow()', modifiers='public', annotations='@Tag("slow")'))
    inventory = expand(dict(classes=classes, methods=declared))
    def execute(command, cwd, log, timeout):
        assert command == ['mvn', '-o', 'test', '-Dmaven.test.failure.ignore=true']
        reports = folder/'target/surefire-reports'; reports.mkdir(parents=True)
        (reports/'TEST-p.Tests.xml').write_text('<testsuite>'+''.join(
            f'<testcase classname="p.Tests" name="t{i}()"/>' for i in range(450))+'</testsuite>')
        return dict(command=command, exit_code=0, seconds=1)
    config = validate(dict(name='build', anchor='t', project=dict(repo=str(tmp_path)), dev=dict(discovery='build')))
    adapter = SimpleNamespace(exp=tmp_path, config=config, folder=lambda _:folder, runner=SimpleNamespace(execute=execute))
    candidates = discover(adapter, [m for m in inventory['methods'] if m['kind']])
    assert len(candidates) == 450 > config['scope']['max_dev_scope']
    assert all(m['class']=='p.Tests' and m['declaring_class']=='p.Base' and
               m['declaring_file']=='src/test/java/p/Base.java' for m in candidates)
    excluded = read_csv(tmp_path/'dev/dev-not-run-by-build.csv')
    assert any(r['class']=='p.Tests' and r['method']=='slow' and r['reason']=='tag-or-group-exclusion' for r in excluded)
    assert not read_csv(tmp_path/'dev/build-report-unmapped.csv')


def test_failed_build_does_not_accept_stale_surefire_reports(tmp_path):
    folder=tmp_path/'work/checkouts/0';reports=folder/'target/surefire-reports';reports.mkdir(parents=True)
    (folder/'pom.xml').write_text('<project/>')
    (reports/'TEST-p.Tests.xml').write_text('<testsuite><testcase name="stale"/></testsuite>')
    write_json(tmp_path/'results/anchor.json',dict(sha='t'))
    def execute(*args):
        assert not reports.exists()
        return dict(exit_code=1,seconds=1,timeout=False)
    adapter=SimpleNamespace(exp=tmp_path,folder=lambda _:folder,runner=SimpleNamespace(execute=execute),
        config=validate(dict(name='x',anchor='t',project=dict(repo=str(tmp_path)),dev=dict(discovery='build'))))
    with pytest.raises(RuntimeError,match='Build test-phase discovery failed'):
        discover(adapter,[method('stale')])
    assert not read_json(tmp_path/'results/dev-build-discovery.json')['complete']
    assert not (tmp_path/'dev/dev-methods-all.csv').exists()


def test_overloads_are_audited_as_ambiguous_not_as_unreported(tmp_path):
    folder=tmp_path/'work/checkouts/0';folder.mkdir(parents=True);(folder/'pom.xml').write_text('<project/>')
    write_json(tmp_path/'results/anchor.json',dict(sha='anchor'))
    def execute(command,cwd,log,timeout):
        reports=folder/'target/surefire-reports';reports.mkdir(parents=True)
        (reports/'TEST-p.Tests.xml').write_text('<testsuite><testcase name="overloaded(int)[1]"/></testsuite>')
        return dict(command=command,exit_code=0,seconds=1)
    config=validate(dict(name='x',anchor='anchor',project=dict(repo=str(tmp_path)),dev=dict(discovery='build')))
    adapter=SimpleNamespace(exp=tmp_path,config=config,folder=lambda _:folder,runner=SimpleNamespace(execute=execute))
    assert discover(adapter,[method('overloaded'),method('overloaded')])==[]
    assert [r['reason'] for r in read_csv(tmp_path/'dev/dev-not-run-by-build.csv')]==['ambiguous-source-method']*2
    assert read_csv(tmp_path/'dev/build-report-unmapped.csv')[0]['reason']=='ambiguous-source-method'


@pytest.mark.parametrize('legacy_empty', [False, True])
def test_custom_report_directory_cannot_publish_empty_candidates(tmp_path, legacy_empty):
    from test_survival.config import canonical_hash, record_runner_inputs
    folder = tmp_path/'work/checkouts/0'; folder.mkdir(parents=True)
    (folder/'pom.xml').write_text('''<project><build><plugins><plugin>
        <artifactId>maven-surefire-plugin</artifactId><configuration>
        <reportsDirectory>target/qa-xml</reportsDirectory></configuration></plugin></plugins></build></project>''')
    write_json(tmp_path/'results/anchor.json', dict(sha='t'))
    config = validate(dict(name='reports', anchor='t', project=dict(repo=str(tmp_path)), dev=dict(discovery='build')))
    if legacy_empty:
        fingerprint = canonical_hash(dict(sha='t', runner=record_runner_inputs(config['runner']), module='.'))
        write_json(tmp_path/'results/dev-build-discovery.json', dict(input_sha256=fingerprint, complete=True))
        (tmp_path/'results/dev-build-reports').mkdir()
    calls = []
    def execute(*args):
        calls.append(args)
        reports = folder/'target/qa-xml'; reports.mkdir(parents=True)
        (reports/'TEST-p.Tests.xml').write_text('<testsuite><testcase name="works"/></testsuite>')
        return dict(exit_code=0, seconds=1)
    adapter = SimpleNamespace(exp=tmp_path, config=config, folder=lambda _:folder, runner=SimpleNamespace(execute=execute))
    with pytest.raises(RuntimeError, match='Surefire XML reports'):
        discover(adapter, [method('works')])
    assert len(calls) == 1
    assert not read_json(tmp_path/'results/dev-build-discovery.json')['complete']
    assert not (tmp_path/'dev/dev-not-run-by-build.csv').exists()


@pytest.mark.parametrize('saved_empty_repeat', [False, True])
def test_build_stability_repeat_requires_report_evidence(tmp_path, monkeypatch, saved_empty_repeat):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    config = validate(dict(name='repeat', anchor='t', project=dict(repo=str(tmp_path)),
                           dev=dict(discovery='build', stable_runs=2)))
    adapter = JavaMavenAdapter(tmp_path, config)
    folder = adapter.folder(0); (folder/'src/test/java/p').mkdir(parents=True)
    (folder/'pom.xml').write_text('<project/>')
    write_json(tmp_path/'results/anchor.json', dict(sha='t'))
    monkeypatch.setattr(adapter, 'list_production_methods', lambda _:dict(classes={}, methods=[]))
    monkeypatch.setattr(adapter, 'list_test_methods', lambda _:dict(classes={}, methods=[method('works')]))
    monkeypatch.setattr(adapter.cache, 'resolve', lambda *a:None)
    if saved_empty_repeat:
        write_json(tmp_path/'results/baseline-run2.json', dict(index=2, attempts=[]))
        (tmp_path/'results/baseline-reports/run2').mkdir(parents=True)
    calls = []
    def execute(command, *args):
        calls.append(command)
        directory = 'surefire-reports' if len(calls)==1 else 'qa-xml'
        reports = folder/'target'/directory; reports.mkdir(parents=True, exist_ok=True)
        (reports/'TEST-p.Tests.xml').write_text('<testsuite><testcase name="works"/></testsuite>')
        return dict(exit_code=0, seconds=1)
    monkeypatch.setattr(adapter.runner, 'execute', execute)
    with pytest.raises(RuntimeError, match='Surefire XML reports'):
        adapter.baseline()
    assert len(calls) == 2
    assert not (tmp_path/'dev/dev-methods-all.csv').exists()


def test_skipped_only_xml_is_valid_evidence_for_empty_candidates(tmp_path):
    folder = tmp_path/'work/checkouts/0'; folder.mkdir(parents=True)
    (folder/'pom.xml').write_text('<project/>')
    write_json(tmp_path/'results/anchor.json', dict(sha='t'))
    def execute(*args):
        reports = folder/'target/surefire-reports'; reports.mkdir(parents=True)
        (reports/'TEST-p.Tests.xml').write_text('<testsuite><testcase name="disabled"><skipped/></testcase></testsuite>')
        return dict(exit_code=0, seconds=1)
    config = validate(dict(name='skipped', anchor='t', project=dict(repo=str(tmp_path)), dev=dict(discovery='build')))
    adapter = SimpleNamespace(exp=tmp_path, config=config, folder=lambda _:folder, runner=SimpleNamespace(execute=execute))
    assert discover(adapter, [method('disabled')])==[]
    record = read_json(tmp_path/'results/dev-build-discovery.json')
    assert record['complete'] and record['counts']['skipped']==1 and record['counts']['excluded']==1


def test_configured_exclusions_leave_candidates_and_are_audited(tmp_path):
    folder = tmp_path/'work/checkouts/0/module';folder.mkdir(parents=True)
    (folder/'pom.xml').write_text('<project/>')
    write_json(tmp_path/'results/anchor.json',dict(sha='anchor'))
    def execute(command,cwd,log,timeout):
        reports = folder/'target/surefire-reports';reports.mkdir(parents=True)
        (reports/'TEST-p.Tests.xml').write_text('<testsuite><testcase name="x"/><testcase name="y"/><testcase name="z"/></testsuite>')
        return dict(command=command,exit_code=0,seconds=1.0)
    raw=dict(name='fixture',anchor='anchor',project=dict(repo=str(tmp_path),module='module'),
             dev=dict(discovery='build',exclude=['p.Tests#y','q.Other']))
    config=validate(raw)
    adapter=SimpleNamespace(exp=tmp_path,config=config,folder=lambda _:folder,runner=SimpleNamespace(execute=execute))
    candidates=[method('x'),method('y'),method('z'),dict(method('w'),**{'class':'q.Other'})]
    assert [m['method'] for m in discover(adapter,candidates)] == ['x','z']
    rows=read_csv(tmp_path/'dev/dev-not-run-by-build.csv')
    assert [(r['class'],r['method'],r['reason']) for r in rows] == [('p.Tests','y','excluded-by-configuration'),('q.Other','w','excluded-by-configuration')]
    assert read_json(tmp_path/'results/dev-build-discovery.json')['excluded_by_reason'] == {'excluded-by-configuration':2}
    # the list is a result-affecting prepare input; an empty list is the old behaviour
    assert stage_hash(config,'prepare') != stage_hash(validate({**raw,'dev':{'discovery':'build'}}),'prepare')
    assert validate({**raw,'dev':{'discovery':'build'}})['dev']['exclude'] == []


@pytest.mark.parametrize('discovery', [None, 'source'])
@pytest.mark.parametrize('exclude', [[], ['p.Tests#runaway', 'q.Other'], ['p.Tests', 'q.Other']])
def test_source_baseline_exclusions_preserve_mapping_and_audit(tmp_path, monkeypatch, discovery, exclude):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.rules.dev_denominator import denominator
    dev = dict(exclude=exclude, stable_runs=1)
    if discovery is not None:
        dev['discovery'] = discovery
    config = validate(dict(name='source', anchor='t', project=dict(repo=str(tmp_path)), dev=dev))
    adapter = JavaMavenAdapter(tmp_path, config)
    (adapter.folder(0)/'src/test/java').mkdir(parents=True)
    methods = [method('safe'), method('runaway'), dict(method('other'), **{'class':'q.Other'})]
    monkeypatch.setattr(adapter, 'list_production_methods', lambda _:dict(classes={}, methods=[]))
    monkeypatch.setattr(adapter, 'list_test_methods', lambda _:dict(classes={}, methods=methods))
    monkeypatch.setattr(adapter.runner, 'execute', lambda *a:pytest.fail('Saved baseline evidence must be reused'))
    write_json(tmp_path/'results/baseline-run1.json', dict(index=1))
    reports = tmp_path/'results/baseline-reports/run1'; reports.mkdir(parents=True)
    (reports/'TEST-p.Tests.xml').write_text('<testsuite><testcase name="safe"/><testcase name="runaway[1]"/></testsuite>')
    (reports/'TEST-q.Other.xml').write_text('<testsuite><testcase name="other"/></testsuite>')
    expected = [] if 'p.Tests' in exclude else ['safe'] if exclude else ['safe', 'runaway', 'other']
    rows = adapter.baseline()
    assert [r['method'] for r in denominator(rows, 'project', [], set())] == expected
    assert read_json(tmp_path/'results/baseline-unmapped.json') == []
    audit = read_csv(tmp_path/'dev/dev-not-run-by-build.csv')
    assert [r['method'] for r in audit] == [m['method'] for m in methods if m['method'] not in expected]
    assert all(r['reason'] == 'excluded-by-configuration' for r in audit)
    # Removing exclusions clears the audit and restores the original population.
    config['dev']['exclude'] = []
    assert len(adapter.baseline()) == 3
    assert read_csv(tmp_path/'dev/dev-not-run-by-build.csv') == []
