"""Bounded coverage launch contracts; fake JVMs, no experiments."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_policy import baseline, test_test
from test_survival.adapters import policy_coverage
from test_survival.config import runner_inputs, stage_hash
from test_survival.io import read_csv, read_json, write_csv, write_json
from test_survival.policy.baseline import Baseline
from test_survival.policy.pipeline import Steps
from test_survival.policy.plan import build_plan


@pytest.mark.parametrize('classes,expected', [
    (['com.networknt.schema.JsonSchema', 'com.networknt.schema.Validator$Inner'], 'com.networknt.schema.*'),
    (['org.example.B', 'com.networknt.schema.A', 'org.example.C'], 'com.networknt.schema.*:org.example.*'),
    (['Production'], 'Production:Production$*'),
    ([], ''),
])
def test_includes_pattern(classes, expected):
    assert policy_coverage.includes_pattern(classes) == expected


@pytest.mark.parametrize('failure', [None, 'exit', 'timeout'])
@pytest.mark.parametrize('override', [None, '3g'])
def test_chunks_preserve_successful_coverage_and_limit_fallback(tmp_path, monkeypatch, failure, override):
    if override:
        monkeypatch.setenv('SURVIVAL_JVM_JACOCO', override)
    else:
        monkeypatch.delenv('SURVIVAL_JVM_JACOCO', raising=False)
    monkeypatch.setattr(policy_coverage, 'filtered_roots', lambda exp, methods, *args: methods)
    monkeypatch.setattr(policy_coverage, 'compile_snapshot', lambda *a, **kw: (tmp_path/'classes', {'exit_code':0}))
    tests = [dict(id=str(i), file=f'pool/{i}/Test.java', source_root=f'pool/{i}', file_hash=str(i),
                  execution_hash=str(i), **{'class':'Test'}, method='works', lines=[['stale.java', 999]]) for i in range(18)]
    calls = []; fallback = []
    def execute(command, cwd, log, timeout):
        if 'PerTestCoverage' not in command:
            return dict(exit_code=0)
        rows = Path(command[-1]).read_text().splitlines()
        calls.append((command, rows, timeout))
        failed = len(calls) == 2 and failure
        # Even a failed JVM can leave partial dumps. They must be discarded.
        for row in rows:
            output, _, _, *_ = row.split('\t')
            output = Path(output); output.mkdir(parents=True)
            (output/'tests.tsv').write_text('0\tTest\tworks\tSUCCESSFUL\t\n1\tTest\tworks\tSUCCESSFUL\t\n')
            (output/'0.tsv').write_text('src/main/java/p/Production.java\t1\ttrue\noutside.java\t7\ttrue\n')
            (output/'1.tsv').write_text('src/main/java/p/Production.java\t2\ttrue\n')
        return dict(exit_code=1 if failed and failure == 'exit' else 0,
                    timeout=bool(failed and failure == 'timeout'), log=str(log.relative_to(tmp_path)))
    def run_file(index, group, filename, members, part, **kwargs):
        fallback.extend(t['id'] for t in members)
        assert kwargs == dict(retry=False, environment_retry=False)
        return dict(methods=[dict(t, state='pass') for t in members])
    w = SimpleNamespace(EXP=tmp_path, HELPERS=tmp_path, JVM={'jacoco':'1g'},
                        TIMEOUTS={'compile':30, 'test_class':10}, classpath=lambda _: (tmp_path, ''),
                        jacoco_version=lambda _: '0.8.14', execute=execute, run_file=run_file, write_json=write_json)
    policy_coverage.run(w, dict(index=107, sha='sha', methods=tests, survival=True, work='work/job', output='result.json',
                                scope_files=['src/main/java/p/Production.java'], scope_classes=['p.Production']))
    result = read_json(tmp_path/'result.json')
    assert [len(rows) for _, rows, _ in calls] == [8, 8, 2]
    assert [timeout for _, _, timeout in calls] == [80, 80, 20]
    assert all('-Xmx'+(override or '1g') in command for command, _, _ in calls)
    assert {next(a for a in command if a.startswith('-javaagent:')) for command, _, _ in calls} == {
        f'-javaagent:{tmp_path}/work/libs/org.jacoco.agent-0.8.14-runtime.jar=output=none,dumponexit=false,includes=p.*'}
    assert result['coverage_status'] == ('missing' if failure else 'complete')
    assert fallback == ([str(i) for i in range(8, 16)] if failure else [])
    assert len(result['methods']) == 18
    for t in result['methods']:
        if t['id'] in fallback:
            assert 'lines' not in t
        else:
            assert t['lines'] == [['src/main/java/p/Production.java', 1], ['src/main/java/p/Production.java', 2]]
    assert result['universe'] == [['src/main/java/p/Production.java', 1], ['src/main/java/p/Production.java', 2]]
    assert result['jvm'] == dict(configured='1g', effective=override or '1g',
                                 environment={'SURVIVAL_JVM_JACOCO':'3g'} if override else {})
    assert read_json(tmp_path/'work/job/coverage-jvm.json')['jvm'] == result['jvm']
    assert w.JVM == {'jacoco':'1g'}
    assert [r['groups'] for r in result['runs']] == [8, 8, 2]


def test_heap_override_is_forwarded_and_recorded_without_hash_changes(baseline, monkeypatch):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    exp, config, _ = baseline
    b = Baseline(exp, config); plan = build_plan(b)
    adapter = JavaMavenAdapter(exp, b.execution_config)
    before = deepcopy(b.execution_config)
    hashes = lambda: ([stage_hash(b.execution_config, stage) for stage in
                       ('prepare', 'scope', 'generate', 'screen', 'control', 'survive', 'cover')],
                      Steps(b, plan).digest, adapter.build_fingerprint('sha'),
                      deepcopy(runner_inputs(b.execution_config['runner'])), adapter.provenance())
    monkeypatch.delenv('SURVIVAL_JVM_JACOCO', raising=False)
    original_hashes = hashes()
    calls = []
    def execute(command, *args):
        calls.append(command)
        return dict(exit_code=0)
    monkeypatch.setattr(adapter.runner, 'execute', execute)
    for override in (None, '3g'):
        if override:
            monkeypatch.setenv('SURVIVAL_JVM_JACOCO', override)
        adapter.worker(dict(action='policy-survive', index=1, methods=[], work='work/heap'))
        evidence = read_json(exp/'work/heap-job.json')
        assert evidence['jvm'] == before['runner']['jvm']
        assert evidence.get('coverage_environment', {}) == ({'SURVIVAL_JVM_JACOCO':'3g'} if override else {})
        assert calls[-1][:2] == (['env', 'SURVIVAL_JVM_JACOCO=3g'] if override else ['python3', str(Path(policy_coverage.__file__).with_name('java_worker.py'))])
        assert hashes() == original_hashes
        assert b.execution_config == before


def test_only_failed_groups_appear_in_missing_csv(baseline, monkeypatch):
    from test_survival.policy import execute, cover
    exp, config, root = baseline
    b = Baseline(exp, config); b.groups = ['good', 'bad']; b.dev = {}
    b.options['set'] = [dict(name='reset', trigger='release', action='reset')]
    plan = dict(points=[dict(group=g, index=1, trigger='release') for g in b.groups],
                pools=[dict(group=g, index=1) for g in b.groups])
    monkeypatch.setattr(execute, 'baseline_tests', lambda _: {g:[] for g in b.groups})
    monkeypatch.setattr(b, 'raw', lambda *a: {'methods':[]})
    monkeypatch.setattr(execute.JavaMavenAdapter, 'build', lambda *a: {'build':'ok'})
    monkeypatch.setattr(execute.JavaMavenAdapter, 'setup_runner', lambda *a: None)
    def worker(self, job):
        assert job['scope_classes'] == ['A']  # Read from population/scope.json.
        return dict(coverage_status='missing', coverage_reason='batch JVM failed or timed out',
                    methods=[dict(t, state='pass', lines=[['src/main/java/A.java', 1]]) if t['id']=='good'
                             else dict(t, state='fail') for t in job['methods']])
    monkeypatch.setattr(execute.JavaMavenAdapter, 'worker', worker)
    pools = {(g,1):[test_test(g)] for g in b.groups}
    execute.survive(b, plan, pools, Steps(b, plan))
    out = exp/'results/policy/reset'
    expected = [dict(index='2', sha=b.by_index[2]['sha'], group='bad', reason='batch JVM failed or timed out')]
    assert read_csv(out/'coverage-missing.csv') == expected
    rows = [dict(index=i, sha=b.by_index[i]['sha'], date='date', subject='change', scope_executable=1) for i in (2,3)]
    write_csv(root/'results/changed-lines.csv', rows, list(rows[0]))
    for i in (2,3):
        write_json(root/f'results/changes/{i}.json', dict(post_image={'src/main/java/A.java':[1]}))
    cover.run(b, None)
    assert read_csv(out/'coverage-missing.csv') == expected
    rows = read_csv(out/'coverage-commits.csv')
    assert all(r['good_covered']=='1' for r in rows)
    assert [r['bad_covered'] for r in rows] == ['', '0']
