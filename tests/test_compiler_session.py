import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import zipfile

import pytest

from test_survival.adapters.compiler_session import CompilerSession, EXTERNAL_OPTIONS, MAGIC
from test_survival.config import validate, record_runner_inputs, stage_hash
from test_junit4 import java_environment


@pytest.fixture
def session_input(tmp_path, monkeypatch):
    for name in EXTERNAL_OPTIONS:
        monkeypatch.delenv(name, raising=False)
    sourcepath = tmp_path/'source'
    sourcepath.mkdir()
    source = sourcepath/'Test.java'
    source.write_text('class Test {}')
    cp = tmp_path/'production'
    cp.mkdir()
    output = tmp_path/'work/classes'
    output.mkdir(parents=True)
    command = ['javac', '-J-Xmx512m', '-cp', str(cp), '-sourcepath', str(sourcepath), '-d', str(output), str(source)]
    calls = []
    def fallback(command, cwd, log, timeout):
        calls.append(command)
        assert not (output/'Partial.class').exists()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('fallback diagnostic')
        return dict(command=command, cwd=str(cwd), exit_code=0, seconds=.01, timeout=False,
                    log=str(log.relative_to(tmp_path)))
    return tmp_path, command, calls, fallback


@pytest.mark.parametrize('unsupported', ['environment', 'wildcard', 'argfile', 'option', 'processor_option', 'plugin', 'manifest', 'outside'])
def test_unsupported_requests_use_original_subprocess(session_input, monkeypatch, unsupported):
    root, command, calls, fallback = session_input
    if unsupported == 'environment':
        monkeypatch.setenv('JDK_JAVAC_OPTIONS', '-verbose')
    elif unsupported == 'wildcard':
        command[3] += '/*'
    elif unsupported == 'argfile':
        command[-1] = '@args.txt'
    elif unsupported == 'option':
        command.insert(2, '-Xmaxerrs')
    elif unsupported == 'processor_option':
        command.insert(2, '-proc:full')
    elif unsupported in ('plugin', 'manifest'):
        jar = root/'extension.jar'
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.writestr('META-INF/services/com.sun.source.util.Plugin' if unsupported == 'plugin'
                             else 'META-INF/MANIFEST.MF', 'Plugin' if unsupported == 'plugin' else 'Class-Path: extra.jar\n')
        command[3] = str(jar)
    else:
        command[7] = str(root/'production')
    with CompilerSession(root, fallback, 'reused-jvm') as session:
        result = session.execute(command, root, root/'compile.txt', 5)
        assert session.child is None
    assert result['compiler_backend']['mode'] == 'subprocess'
    assert calls == [command]


@pytest.mark.parametrize('failure', ['timeout', 'exit', 'protocol', 'task_fault'])
def test_bad_worker_is_discarded_and_partial_outputs_are_removed(session_input, monkeypatch, failure):
    root, command, calls, fallback = session_input
    # Use a real child process/pipe but no JDK for lifecycle/protocol fault injection.
    monkeypatch.setattr('test_survival.adapters.compiler_session.shutil.which', lambda _: sys.executable)
    sibling = Path(sys.executable).resolve().with_name('java')
    original_stat = Path.stat
    monkeypatch.setattr(Path, 'stat', lambda p, *a, **kw: original_stat(Path(sys.executable) if p == sibling else p, *a, **kw))
    worker_code = '''
import os, struct, sys, time
data=sys.stdin.buffer
ident,count=struct.unpack('>ii',data.read(8))
values=[]
for _ in range(count):
 size=struct.unpack('>i',data.read(4))[0]; values.append(data.read(size).decode())
open(values[3]+'/Partial.class','w').write('partial')
mode=sys.argv[1]
if mode=='timeout': time.sleep(5)
elif mode=='protocol': os.write(1,struct.pack('>iiii',0,ident,0,0))
elif mode=='task_fault': os.write(1,struct.pack('>iiii',0x43534A31,ident,2,1))
'''
    children = []
    def start(session, java, heap, cwd, log, deadline):
        session.child = subprocess.Popen([sys.executable, '-c', worker_code, failure],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True, bufsize=0)
        os.set_blocking(session.child.stdin.fileno(), False)
        children.append(session.child)
    monkeypatch.setattr(CompilerSession, '_start', start)
    (root/'compile.txt').write_text('obsolete diagnostic from a previous run')
    with CompilerSession(root, fallback, 'reused-jvm') as session:
        result = session.execute(command, root, root/'compile.txt', .2 if failure == 'timeout' else 5)
        assert session.child is None
        assert all(child.poll() is not None for child in children)
        assert not (Path(command[7])/'Partial.class').exists()
        if failure == 'timeout':
            assert result['timeout'] and result['exit_code'] == -9 and not calls
        else:
            assert result['exit_code'] == 0 and len(calls) == 1
        assert result['compiler_backend']['discarded']
        assert 'obsolete diagnostic' not in (root/'compile.txt.jvm-failure').read_text()
        # A later request cannot inherit the discarded worker.
        session.execute(command, root, root/'second.txt', .2 if failure == 'timeout' else 5)
        assert len(children) == 2 and children[0].pid != children[1].pid


def test_mode_is_opt_in_and_separates_result_fingerprints():
    plain = validate(dict(name='x', anchor='t', project={'repo': '/tmp/repo'}))
    enabled = validate(dict(name='x', anchor='t', project={'repo': '/tmp/repo'}, runner={'javac_mode': 'reused-jvm'}))
    assert plain['runner']['javac_mode'] == 'subprocess'
    assert 'javac_mode' not in record_runner_inputs(plain['runner'])
    assert stage_hash(plain, 'survive') != stage_hash(enabled, 'survive')


def test_unqualified_jdk_is_not_restarted_for_every_request(session_input, monkeypatch):
    root, command, calls, fallback = session_input
    monkeypatch.setattr('test_survival.adapters.compiler_session.shutil.which', lambda _: sys.executable)
    sibling = Path(sys.executable).resolve().with_name('java')
    original_stat = Path.stat
    monkeypatch.setattr(Path, 'stat', lambda p, *a, **kw: original_stat(Path(sys.executable) if p == sibling else p, *a, **kw))
    original_popen = subprocess.Popen
    children = []
    def unqualified(args, **kwargs):
        assert args[2:4] == ['-cp', str(Path(__file__).resolve().parents[1]/'src/test_survival/adapters/java')]
        child = original_popen([sys.executable, '-c',
            f'import os,struct; os.write(1,struct.pack(">ii",{MAGIC},21))'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr('test_survival.adapters.compiler_session.subprocess.Popen', unqualified)
    with CompilerSession(root, fallback, 'reused-jvm') as session:
        for number in range(2):
            result = session.execute(command, root, root/f'fallback-{number}.txt', 5)
            assert result['compiler_backend']['mode'] == 'subprocess'
            assert 'requires JDK 25' in result['compiler_backend']['reason']
        assert len(children) == 1 and children[0].poll() is not None
        monkeypatch.setenv('TEST_CHANGED_JDK_ENVIRONMENT', 'changed')
        session.execute(command, root, root/'changed.txt', 5)
        assert len(children) == 2 and all(child.poll() is not None for child in children)
    assert len(calls) == 3


@pytest.mark.java_integration
def test_real_compiler_session_equivalence(tmp_path, java_environment):
    image, _ = java_environment
    root = Path(__file__).resolve().parents[1]
    output = subprocess.check_output(['docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
        '--entrypoint', 'timeout', '-v', str(root)+':/code:ro', '-v', str(tmp_path)+':/experiment',
        '-w', '/experiment', image, '-k', '3s', '100s', 'python3',
        '/code/tests/fixtures/compiler_session_probe.py'], text=True, timeout=110)
    result = json.loads(output)
    assert result['equivalent'] and result['same_pid'] and result['restarted']
    assert result['cache_miss_then_hit'] and result['cache_backend_separated']


@pytest.mark.java_integration
def test_real_parallel_compiler_sessions_are_bounded_and_isolated(tmp_path, java_environment):
    image, _ = java_environment
    root = Path(__file__).resolve().parents[1]
    output = subprocess.check_output(['docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
        '--memory', '2g', '--cpus', '2', '--pids-limit', '128',
        '--entrypoint', 'timeout', '-v', str(root)+':/code:ro', '-v', str(tmp_path)+':/experiment',
        '-w', '/experiment', image, '-k', '3s', '180s', 'python3',
        '/code/tests/fixtures/compiler_session_parallel.py'], text=True, timeout=190)
    result = json.loads(output)
    assert result['equivalent'] and sum(row['compiles'] for row in result['workers']) == 198


@pytest.mark.java_integration
def test_real_group_reuse_preserves_retry_and_coverage(tmp_path, monkeypatch, java_environment):
    import test_junit4
    original_validate = test_junit4.validate
    original_run = test_junit4.JavaMavenAdapter.run_group
    results = []
    adapters = []
    def enabled(data):
        data.setdefault('runner', {})['javac_mode'] = 'reused-jvm'
        return original_validate(data)
    def recorded(adapter, *args, **kwargs):
        result = original_run(adapter, *args, **kwargs)
        results.append(result)
        adapters.append(adapter)
        return result
    monkeypatch.setattr(test_junit4, 'validate', enabled)
    monkeypatch.setattr(test_junit4.JavaMavenAdapter, 'run_group', recorded)
    test_junit4.test_real_worker_environment_retries_and_single_method_coverage(tmp_path, java_environment)
    assert results[0]['whole_frozen_suite_compile']['compiler_backend']['mode'] == 'reused-jvm'
    # A failed suite must still run the compilable file and preserve the failed denominator.
    broken = tmp_path/'dev/src-test-t/java/fixture/Broken.java'
    broken.write_text('package fixture; public class Broken { Missing value; }')
    rows = [dict(file='dev/src-test-t/java/fixture/'+name+'.java',
                 **{'class': 'fixture.'+name}, method=method, kind='plain')
            for name, method in [('EnvironmentProbe', 'transientBind'), ('Broken', 'test')]]
    for row in rows:
        row['id'] = '|'.join(row[k] for k in ('file', 'class', 'method'))
    adapter = adapters[0]
    comparisons = []
    for mode in ('reused-jvm', 'subprocess'):
        adapter.config['runner']['javac_mode'] = mode
        with adapter.runner.session('mixed-fixture'):
            raw = adapter.run_group(0, 'dev', rows)
        assert raw['whole_frozen_suite_compile']['exit_code'] == 1
        assert len(raw['methods']) == 2
        comparisons.append({m['id']: m['state'] for m in raw['methods']})
        assert sorted(f['compile']['exit_code'] for f in raw['files']) == [0, 1]
        if mode == 'reused-jvm':
            events = [raw['whole_frozen_suite_compile'], *(f['compile'] for f in raw['files'])]
            assert all(e['compiler_backend']['mode'] == 'reused-jvm' for e in events)
            assert len({e['compiler_backend']['pid'] for e in events}) == 1
    assert comparisons[0] == comparisons[1]
    assert comparisons[0][rows[0]['id']] == 'pass'
    assert comparisons[0][rows[1]['id']] == 'compile-fail'
