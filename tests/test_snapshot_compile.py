"""Snapshot helper inventory must survive unparsable leftover sources in a pool root."""
from pathlib import Path
from types import SimpleNamespace

from test_survival.adapters.snapshot_compile import compile_snapshot


def fake_worker(tmp_path, inventory_rows, javac_exit):
    """A worker whose `java` call writes the inventory and whose `javac` reports a fixed exit code."""
    calls = []

    def execute(command, cwd, log, timeout):
        calls.append(command)
        log.parent.mkdir(parents=True, exist_ok=True)
        if command[0] == 'java':
            log.write_text('\n'.join('\t'.join(map(str, r)) for r in inventory_rows) + '\n')
            return dict(exit_code=0, timeout=False)
        log.write_text('')
        return dict(exit_code=javac_exit(command), timeout=False)

    return SimpleNamespace(execute=execute, HELPERS=Path('/helpers'), JVM={'javac': '512m'},
                           TIMEOUTS={'compile': 300}), calls


def test_unparsable_leftover_is_skipped_and_excluded_from_helpers(tmp_path):
    root = tmp_path/'pool'
    (root/'temp').mkdir(parents=True)
    kept = root/'KeptTest.java'; kept.write_text('class KeptTest { @Test void t() {} }')
    helper = root/'Helper.java'; helper.write_text('class Helper {}')
    broken = root/'temp'/'Broken_0.java'; broken.write_text('class Broken_0 { String s = """x"""; }')
    rows = [('C', 'KeptTest', 'CLASS', 'true', '', str(kept)),
            ('M', 'KeptTest', 't', 'plain', 'true', 't()', '', 1, 'h'),
            ('C', 'Helper', 'CLASS', 'true', '', str(helper)),
            ('X', str(broken), 'temp/Broken_0.java:1: error: illegal text block open delimiter sequence')]
    # The whole-root attempt fails (an unrelated test broke); the fallback must succeed.
    w, calls = fake_worker(tmp_path, rows, lambda cmd: 1 if 'whole-snapshot' in str(cmd) else 0)
    classes, result = compile_snapshot(w, root, [kept], tmp_path/'part', 'cp', tmp_path)
    inventory_call = next(c for c in calls if c[0] == 'java')
    assert inventory_call[-1] == 'lenient'
    assert result['exit_code'] == 0 and result['snapshot_full_suite'] is False
    assert result['helper_sources'] == ['Helper.java']
    assert result['unparsable_sources'] == ['temp/Broken_0.java']
    fallback = calls[-1]
    compiled = next(Path(a[1:]) for a in fallback if str(a).startswith('@')).read_text()
    assert str(kept) in compiled and str(helper) in compiled
    assert str(broken) not in compiled
