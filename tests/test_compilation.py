from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import time
import zipfile

import pytest

from test_survival.adapters.compilation import CompilationService


@pytest.fixture
def compilation(tmp_path, monkeypatch):
    for name in ('JAVA_TOOL_OPTIONS', '_JAVA_OPTIONS', 'JDK_JAVA_OPTIONS', 'JDK_JAVAC_OPTIONS'):
        monkeypatch.delenv(name, raising=False)
    sourcepath = tmp_path/'sources'
    sourcepath.mkdir()
    source = sourcepath/'Test.java'
    source.write_text('class Test {}')
    (sourcepath/'Helper.java').write_text('class Helper {}')
    production = tmp_path/'production'
    production.mkdir()
    (production/'Production.class').write_bytes(b'production v1')
    jar = tmp_path/'dependency.jar'
    with zipfile.ZipFile(jar, 'w') as archive:
        archive.writestr('Library.class', b'dependency v1')
    classpath = os.pathsep.join(map(str, [production, jar]))
    calls = []
    compiler = {'version': 'fixture-javac-1'}
    monkeypatch.setattr(CompilationService, 'compiler_identity', lambda *args: dict(compiler))

    def execute(command, cwd, log, timeout):
        calls.append(command)
        classes = Path(command[command.index('-d')+1])
        classes.mkdir(parents=True, exist_ok=True)
        (classes/'Test.class').write_bytes(b'compiled fixture')
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('compiler diagnostic\n')
        return dict(command=command, cwd=str(cwd), exit_code=0, timeout=False,
                    seconds=2.5, log=str(log.relative_to(tmp_path)))

    def run(label, *, sha='commit', heap='512m', timeout=300, executor=execute, service=None, classpath_override=None):
        service = service or CompilationService(tmp_path, executor, dict(sha=sha, group='dev', phase=label, index=1))
        return service.compile([source], sourcepath, classpath if classpath_override is None else classpath_override, tmp_path/label/'classes',
                               tmp_path, tmp_path/label/'javac.txt', heap, timeout)

    return tmp_path, run, calls, compiler, execute


def test_reuses_success_with_zero_command_time_and_durable_execution_evidence(compilation):
    root, run, calls, _, _ = compilation
    first = run('survival')
    shutil.rmtree(root/'survival')  # Worker scratch can be cleared on resume.
    second = run('coverage')
    assert len(calls) == 1
    assert first['cache']['status'] == 'miss' and first['seconds'] == 2.5
    assert second['cache']['status'] == 'hit' and second['seconds'] == 0
    assert (root/'coverage/classes/Test.class').read_bytes() == b'compiled fixture'
    assert (root/second['log']).read_text() == 'compiler diagnostic\n'
    events = [json.loads(p.read_text()) for p in (root/'results/compile-events').glob('*/finished.json')]
    assert sorted(e['executed'] for e in events) == [False, True]
    assert all((root/e['result']['log']).read_text() == 'compiler diagnostic\n' for e in events)
    (root/'coverage/classes/Test.class').write_bytes(b'test changed its own classes')
    assert run('control')['cache']['status'] == 'hit'
    assert (root/'control/classes/Test.class').read_bytes() == b'compiled fixture'


@pytest.mark.parametrize('changed', ['source', 'helper', 'production', 'jar', 'jdk', 'heap', 'timeout', 'commit', 'mtime'])
def test_invalidates_when_any_compile_input_changes(compilation, changed):
    root, run, calls, compiler, _ = compilation
    run('first')
    options = {}
    files = {'source': root/'sources/Test.java', 'helper': root/'sources/Helper.java',
             'production': root/'production/Production.class'}
    if changed in files:
        path = files[changed]
        previous = path.stat()
        # Same size and restored mtime must not hide changed contents.
        path.write_bytes(path.read_bytes().replace(b'1', b'2') if changed == 'production'
                         else path.read_bytes().replace(b'class', b'final'))
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    elif changed == 'jar':
        with zipfile.ZipFile(root/'dependency.jar', 'w') as jar:
            jar.writestr('Library.class', b'dependency v2')
    elif changed == 'jdk':
        compiler['version'] = 'fixture-javac-2'
    elif changed == 'mtime':
        path = root/'sources/Helper.java'
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns+1_000_000))
    else:
        options[{'commit': 'sha'}.get(changed, changed)] = {'heap': '1g', 'timeout': 301, 'commit': 'another-commit'}[changed]
    assert run('second', **options)['cache']['status'] == 'miss'
    assert len(calls) == 2


def test_same_service_observes_changed_files(compilation):
    root, run, calls, _, execute = compilation
    service = CompilationService(root, execute, {'sha': 'commit'})
    run('first', service=service)
    path = root/'production/Production.class'
    previous = path.stat()
    path.write_bytes(b'production v2')
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert run('second', service=service)['cache']['status'] == 'miss'
    assert len(calls) == 2


def test_classpath_order_is_part_of_the_key(compilation):
    root, run, calls, _, _ = compilation
    run('first')
    reversed_classpath = os.pathsep.join(map(str, [root/'dependency.jar', root/'production']))
    assert run('second', classpath_override=reversed_classpath)['cache']['status'] == 'miss'
    assert len(calls) == 2


@pytest.mark.parametrize('exit_code,timed_out', [(1, False), (137, False), (-9, True), (0, True)])
def test_failure_environment_error_and_timeout_are_never_cached(compilation, exit_code, timed_out):
    root, run, calls, _, execute = compilation
    def fail(*args):
        result = execute(*args)
        return {**result, 'exit_code': exit_code, 'timeout': timed_out}
    a, b = run('first', executor=fail), run('second', executor=fail)
    assert len(calls) == 2
    assert a['cache']['status'] == b['cache']['status'] == 'miss'
    assert b['exit_code'] == exit_code and b['timeout'] == timed_out
    assert not list((root/'work/compile-cache').rglob('manifest.json'))


@pytest.mark.parametrize('corruption', ['class', 'manifest', 'diagnostic', 'missing', 'partial', 'file', 'symlink'])
def test_corrupt_or_partial_artifact_is_recompiled(compilation, corruption):
    root, run, calls, _, _ = compilation
    first = run('first')
    entry = root/'work/compile-cache/v1'/first['cache']['key']
    if corruption == 'class':
        (entry/'classes/Test.class').write_bytes(b'corruption')
    elif corruption == 'manifest':
        (entry/'manifest.json').write_text('{')
    elif corruption == 'diagnostic':
        (entry/'javac.txt').write_text('corruption')
    elif corruption == 'missing':
        (entry/'classes/Test.class').unlink()
    elif corruption == 'partial':
        entry.rename(entry.with_name(entry.name+'.interrupted'))
    elif corruption == 'file':
        shutil.rmtree(entry)
        entry.write_text('not a directory')
    else:
        saved = entry.with_name(entry.name+'.saved')
        entry.rename(saved)
        entry.symlink_to(saved, target_is_directory=True)
    assert run('second')['cache']['status'] == 'miss'
    assert len(calls) == 2
    assert run('third')['cache']['status'] == 'hit'


@pytest.mark.parametrize('unsupported', ['processor', 'plugin', 'manifest_classpath', 'options', 'symlink'])
def test_untracked_inputs_bypass_cache_without_changing_javac(compilation, monkeypatch, unsupported):
    root, run, calls, _, _ = compilation
    if unsupported in ('processor', 'plugin', 'manifest_classpath'):
        with zipfile.ZipFile(root/'dependency.jar', 'a') as jar:
            name = {'processor': 'META-INF/services/javax.annotation.processing.Processor',
                    'plugin': 'META-INF/services/com.sun.source.util.Plugin',
                    'manifest_classpath': 'META-INF/MANIFEST.MF'}[unsupported]
            jar.writestr(name, 'SomeExtension' if unsupported != 'manifest_classpath'
                         else 'Manifest-Version: 1.0\nClass-Path: other.jar\n')
    elif unsupported == 'options':
        monkeypatch.setenv('JDK_JAVAC_OPTIONS', '-processor SomeProcessor')
    else:
        (root/'sources/link').symlink_to(root/'production', target_is_directory=True)
    assert run('first')['cache']['status'] == run('second')['cache']['status'] == 'bypass'
    assert len(calls) == 2
    assert all('-proc:none' not in command for command in calls)


def test_input_changed_during_compilation_is_not_published(compilation):
    root, run, _, _, execute = compilation
    def changing(*args):
        result = execute(*args)
        (root/'sources/Helper.java').write_text('class Helper { int changed; }')
        return result
    result = run('first', executor=changing)
    assert result['exit_code'] == 0
    assert result['cache']['reason'] == 'inputs changed during compilation'
    assert not list((root/'work/compile-cache').rglob('manifest.json'))


def test_inputs_changed_while_waiting_never_restore_old_classes(compilation):
    root, run, calls, _, execute = compilation
    run('first')
    service = CompilationService(root, execute, {'sha': 'commit'})
    @contextmanager
    def changed_while_waiting(key):
        (root/'production/Production.class').write_bytes(b'production v2')
        yield
    service.locked = changed_while_waiting
    second = run('second', service=service)
    assert second['cache']['status'] == 'bypass'
    assert second['cache']['reason'] == 'inputs changed while waiting for cache'
    assert len(calls) == 2


def test_inputs_changed_during_restore_run_compiler_instead(compilation, monkeypatch):
    root, run, calls, _, _ = compilation
    run('first')
    original = shutil.copyfile
    def copy(source, target, **kwargs):
        result = original(source, target, **kwargs)
        if Path(target) == root/'second/javac.txt':
            (root/'sources/Helper.java').write_text('class Helper { int changed; }')
        return result
    monkeypatch.setattr(shutil, 'copyfile', copy)
    second = run('second')
    assert second['cache']['status'] == 'bypass'
    assert second['cache']['reason'] == 'inputs changed during restore'
    assert len(calls) == 2


def test_interrupted_compile_leaves_evidence_but_no_cache(compilation):
    root, run, _, _, _ = compilation
    def interrupted(*args):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run('first', executor=interrupted)
    assert len(list((root/'results/compile-events').glob('*/started.json'))) == 1
    assert len(list((root/'results/compile-events').glob('*/interrupted.json'))) == 1
    assert not list((root/'work/compile-cache').rglob('manifest.json'))
    assert run('resumed')['cache']['status'] == 'miss'


@pytest.mark.parametrize('parallelism', ['threads', 'processes'])
def test_same_key_is_compiled_once_under_concurrency(compilation, parallelism):
    root, run, _, _, execute = compilation
    def slow(*args):
        with (root/'subprocess-count').open('a') as stream:
            stream.write('executed\n')
        time.sleep(.05)
        return execute(*args)
    def job(label):
        run(label, executor=slow)
    if parallelism == 'threads':
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(job, ['one', 'two']))
    else:
        context = multiprocessing.get_context('fork')
        children = [context.Process(target=job, args=(label,)) for label in ['one', 'two']]
        for child in children:
            child.start()
        for child in children:
            child.join(10)
            assert child.exitcode == 0
    assert (root/'subprocess-count').read_text().splitlines() == ['executed']
    events = [json.loads(p.read_text()) for p in (root/'results/compile-events').glob('*/finished.json')]
    assert sorted(e['result']['cache']['status'] for e in events) == ['hit', 'miss']


def test_toolchain_identity_hashes_compiler_contents(tmp_path, monkeypatch):
    home = tmp_path/'jdk'
    for name in ['bin/javac', 'release', 'lib/modules', 'lib/ct.sym']:
        p = home/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'version1')
    monkeypatch.setattr(shutil, 'which', lambda _: str(home/'bin/javac'))
    def probe(command, cwd, log, timeout):
        log.write_text(f'    java.home = {home}\njavac 25\n')
        return {'exit_code': 0, 'timeout': False}
    service = CompilationService(tmp_path, probe, {'sha': 'commit'})
    first = service.compiler_identity(tmp_path, tmp_path, 300)
    (home/'lib/modules').write_bytes(b'version2')
    second = service.compiler_identity(tmp_path, tmp_path, 300)
    assert first != second
