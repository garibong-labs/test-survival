"""Exercise worker orchestration with a counted compiler and deterministic JVM reports."""
import json
from pathlib import Path

import pytest

from test_survival.adapters import java_worker as worker
from test_survival.adapters.compilation import CompilationService


@pytest.fixture
def group_worker(tmp_path, monkeypatch):
    for name in ('JAVA_TOOL_OPTIONS', '_JAVA_OPTIONS', 'JDK_JAVA_OPTIONS', 'JDK_JAVAC_OPTIONS'):
        monkeypatch.delenv(name, raising=False)
    sources = tmp_path/'dev/src-test-t/java'
    sources.mkdir(parents=True)
    for name in ['Good', 'Bad']:
        (sources/f'{name}.java').write_text(f'class {name} {{}}')
    production = tmp_path/'target/classes'
    production.mkdir(parents=True)
    (production/'Production.class').write_bytes(b'production')
    for name, value in dict(EXP=tmp_path, SHARED=tmp_path, REPO=tmp_path, DEVELOPER=True,
                            JVM={'javac': '512m', 'launcher': '1g', 'jacoco': '1g'},
                            TIMEOUTS={'compile': 300, 'test_class': 120}, FLAKY_RERUN=1,
                            ENVIRONMENT_ERRORS={'patterns': []}).items():
        monkeypatch.setattr(worker, name, value, raising=False)
    monkeypatch.setattr(worker, 'classpath', lambda _: (tmp_path, str(production)))
    monkeypatch.setattr(CompilationService, 'compiler_identity', lambda *args: {'version': 'fixture'})
    monkeypatch.setattr(worker, 'selectors', lambda rows, *args: {r['id']: [r['class']+'#test'] for r in rows})
    monkeypatch.setattr(worker, 'parse_reports', lambda _: {('Good', 'test'): [{'state': 'pass', 'message': ''}]})
    calls = []
    def execute(command, cwd, log, timeout):
        calls.append(command)
        log.parent.mkdir(parents=True, exist_ok=True)
        failed = command[0] == 'javac' and any(str(arg).endswith('/Bad.java') for arg in command)
        log.write_text('Bad.java: error: cannot find symbol\n' if failed else '')
        if command[0] == 'javac' and not failed:
            (Path(command[command.index('-d')+1])/'Good.class').write_bytes(b'compiled Good')
        if '--xml' in command:
            path = Path(command[command.index('--xml')+1])
            path.write_text('<report><package name="p"><sourcefile name="Production.java">'
                            '<line nr="1" mi="1" ci="0"/></sourcefile></package></report>')
        return dict(command=command, cwd=str(cwd), exit_code=int(failed), timeout=False,
                    seconds=.1, log=str(log.relative_to(tmp_path)))
    monkeypatch.setattr(worker, 'execute', execute)
    rows = [dict(id=name, file=str((sources/f'{name}.java').relative_to(tmp_path)),
                 **{'class': name}, method='test', kind='plain') for name in ['Good', 'Bad']]
    def run(phase, methods=rows):
        output = f'results/{phase}.json'
        worker.run_group(dict(index=0, sha='anchor', group='dev', phase=phase, methods=methods,
                              work=f'work/{phase}', output=output, input_sha256='input', runner_sha256={}))
        return json.loads((tmp_path/output).read_text())
    return tmp_path, run, calls


def test_failed_suite_preserves_fallback_and_only_reuses_success(group_worker):
    root, run, calls = group_worker
    first = run('A')
    second = run('B')
    for result in [first, second]:
        assert result['whole_frozen_suite_compile']['exit_code'] == 1
        assert result['whole_frozen_suite_compile']['source_count'] == 2
        assert {r['id']: r['state'] for r in result['methods']} == {'Good': 'pass', 'Bad': 'compile-fail'}
        bad = next(f for f in result['files'] if f['methods'][0]['id'] == 'Bad')
        assert bad['methods'][0]['message'] == 'Bad.java: error: cannot find symbol\n'
        assert bad['compile']['cache']['status'] == 'miss'
    good = next(f for f in second['files'] if f['methods'][0]['id'] == 'Good')
    assert good['compile']['cache']['status'] == 'hit'
    assert len([c for c in calls if c[0] == 'javac']) == 5  # 3 + 2, not 3 + 3.
    assert len(good['runs']) == 1  # Cached compilation never skips test execution.
    assert first['requested_ids'] == second['requested_ids'] == ['Good', 'Bad']
    assert second['status'] == 'evaluated'


def test_empty_coverage_group_skips_compile_but_preserves_line_universe(group_worker):
    root, run, calls = group_worker
    result = run('B', [])
    assert not any(c[0] == 'javac' for c in calls)
    assert 'whole_frozen_suite_compile' not in result
    assert result['files'] == result['methods'] == result['requested_ids'] == []
    assert result['status'] == 'evaluated' and result['coverage_report']['exit_code'] == 0
    assert (root/'results/coverage/0-dev-lines.csv').read_text() == (
        'file,line,covered\nsrc/main/java/p/Production.java,1,no\n')


def test_successful_suite_is_shared_and_resume_keeps_execution_history(group_worker):
    root, run, calls = group_worker
    (root/'dev/src-test-t/java/Bad.java').unlink()
    row = dict(id='Good', file='dev/src-test-t/java/Good.java', **{'class': 'Good'}, method='test', kind='plain')
    first = run('A', [row])
    second = run('B', [row])
    resumed = run('A', [row])  # Clears the A scratch directory, not the execution ledger.
    assert first['whole_frozen_suite_compile']['cache']['status'] == 'miss'
    assert second['whole_frozen_suite_compile']['cache']['status'] == resumed['whole_frozen_suite_compile']['cache']['status'] == 'hit'
    assert len([c for c in calls if c[0] == 'javac']) == 1
    assert len(list((root/'results/compile-events').glob('*/finished.json'))) == 3
    assert all(r['methods'][0]['state'] == 'pass' for r in [first, second, resumed])
