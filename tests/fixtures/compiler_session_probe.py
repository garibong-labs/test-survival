"""Small real-JDK protocol, isolation and cache probe; writes only /experiment."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import shutil
import zipfile

sys.path.insert(0, '/code/src')
from test_survival.adapters.compiler_session import CompilerSession
from test_survival.adapters import compiler_session
from test_survival.adapters.compilation import CompilationService

root = Path('/experiment')
sources = root/'source'
sources.mkdir()
production = root/'production'
production.mkdir()
output = root/'work/classes'
output.mkdir(parents=True)
source = sources/'Test.java'

def execute(command, cwd, log, timeout):
    log.parent.mkdir(parents=True, exist_ok=True)
    import time
    started = time.monotonic()
    with log.open('w') as stream:
        result = subprocess.run(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
    return dict(command=command, cwd=str(cwd), exit_code=result.returncode, timeout=False,
                seconds=time.monotonic()-started, log=str(log.relative_to(root)))

command = ['javac', '-J-Xmx512m', '-cp', str(production), '-sourcepath', str(sources), '-d', str(output), str(source)]
def snapshot():
    return {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in output.rglob('*') if p.is_file()}

def reset():
    shutil.rmtree(output)
    output.mkdir()

contents = ['class Test { int value() { return 1; } }',
            'class Test { int value() { return Missing.value(); } }',
            'class Test { int value() { return 2; } }']
pids = []
with CompilerSession(root, execute, 'reused-jvm') as session:
    for i, text in enumerate(contents + list(reversed(contents))):
        source.write_text(text)
        reset()
        expected = execute(command, root, root/'cli.txt', 20)
        expected_classes, expected_log = snapshot(), (root/'cli.txt').read_bytes()
        reset()
        actual = session.execute(command, root, root/'api.txt', 20)
        assert actual['compiler_backend']['mode'] == 'reused-jvm', actual
        assert expected['exit_code'] == actual['exit_code']
        assert expected_log == (root/'api.txt').read_bytes()
        assert expected_classes == snapshot()
        pids.append(actual['compiler_backend']['pid'])
    session.requests = 64  # Verify bounded recycling without 64 extra compiles.
    reset()
    restarted = session.execute(command, root, root/'restart.txt', 20)
    assert restarted['compiler_backend']['pid'] != pids[0]
    # A changed cwd must start a new JVM (java.user.dir is not per CompilationTask).
    changed = root/'new-commit'
    changed.mkdir()
    moved = session.execute(command, changed, root/'moved.txt', 20)
    assert moved['compiler_backend']['pid'] != restarted['compiler_backend']['pid']
    source.write_text(contents[0])
    compiler = CompilationService(root, session.execute, {'sha': 'one'}, backend=session.identity)
    first = compiler.compile([source], sources, str(production), output, changed, root/'cached1.txt', '512m', 20)
    second = compiler.compile([source], sources, str(production), output, changed, root/'cached2.txt', '512m', 20)
    assert first['cache']['status'] == 'miss' and second['cache']['status'] == 'hit'
    cli_cache = CompilationService(root, execute, {'sha': 'one'})
    cli = cli_cache.compile([source], sources, str(production), output, changed, root/'cached-cli.txt', '512m', 20)
    assert cli['cache']['status'] == 'miss'
    # Concurrent callers serialize on one protocol; outputs and diagnostics remain private.
    from concurrent.futures import ThreadPoolExecutor
    before_fds = len(list(Path(f'/proc/{session.child.pid}/fd').iterdir()))
    def parallel(number):
        destination = root/f'work/parallel-{number}'
        destination.mkdir()
        args = command.copy()
        args[7] = str(destination)
        result = session.execute(args, changed, root/f'parallel-{number}.txt', 20)
        assert result['exit_code'] == 0 and (destination/'Test.class').is_file()
        return result['compiler_backend']['pid']
    with ThreadPoolExecutor(max_workers=3) as pool:
        ids = list(pool.map(parallel, range(3)))
    assert len(set(ids)) == 1
    assert len(list(Path(f'/proc/{session.child.pid}/fd').iterdir())) <= before_fds+1
    # Replacing classes at the same classpath must not leave stale symbols in the JVM.
    dependency = root/'Dependency.java'
    source.write_text('class Test { int value() { return Dependency.value(); } }')
    for number, signature in enumerate(('int value() { return 1; }', 'int value(int x) { return x; }')):
        dependency.write_text('public class Dependency { public static '+signature+' }')
        built = execute(['javac', '-d', str(production), str(dependency)], root, root/'dependency.txt', 20)
        assert built['exit_code'] == 0
        reset()
        expected = execute(command, changed, root/'cp-cli.txt', 20)
        classes, diagnostic = snapshot(), (root/'cp-cli.txt').read_bytes()
        reset()
        actual = session.execute(command, changed, root/'cp-api.txt', 20)
        assert actual['compiler_backend']['pid'] == ids[0]
        assert actual['exit_code'] == expected['exit_code'] == number
        assert snapshot() == classes and (root/'cp-api.txt').read_bytes() == diagnostic
    # Simulate an updated JDK library stamp without modifying the installed JDK.
    original_stamp = compiler_session.file_stamp
    previous_pid = session.child.pid
    def replaced_release(path):
        stamp = original_stamp(path)
        return (*stamp, 'replacement') if path.name == 'release' and stamp else stamp
    compiler_session.file_stamp = replaced_release
    try:
        session.execute(command, changed, root/'updated-jdk.txt', 20)
        assert session.child.pid != previous_pid
    finally:
        compiler_session.file_stamp = original_stamp
    # A registered processor must remain inactive on both CLI and API defaults.
    processor = root/'SentinelProcessor.java'
    processor.write_text('''
import java.util.Set;
import javax.annotation.processing.*;
import javax.lang.model.SourceVersion;
import javax.lang.model.element.TypeElement;
@SupportedAnnotationTypes("*")
public class SentinelProcessor extends AbstractProcessor {
  public SourceVersion getSupportedSourceVersion() { return SourceVersion.latestSupported(); }
  public boolean process(Set<? extends TypeElement> types, RoundEnvironment round) {
    try { java.nio.file.Files.writeString(java.nio.file.Path.of("/experiment/processor-ran"), "ran"); }
    catch (java.io.IOException e) { throw new RuntimeException(e); }
    throw new IllegalStateException("explicit processor activation");
  }
}
''')
    extension_classes = root/'extension-classes'
    extension_classes.mkdir()
    assert execute(['javac', '-d', str(extension_classes), str(processor)], root, root/'processor-build.txt', 20)['exit_code'] == 0
    processor_jar = root/'processor.jar'
    with zipfile.ZipFile(processor_jar, 'w') as jar:
        jar.write(extension_classes/'SentinelProcessor.class', 'SentinelProcessor.class')
        jar.writestr('META-INF/services/javax.annotation.processing.Processor', 'SentinelProcessor\n')
    processor_command = command.copy()
    processor_command[3] += os.pathsep+str(processor_jar)
    source.write_text(contents[0])
    reset()
    expected = execute(processor_command, changed, root/'processor-cli.txt', 20)
    classes, diagnostic = snapshot(), (root/'processor-cli.txt').read_bytes()
    reset()
    actual = session.execute(processor_command, changed, root/'processor-api.txt', 20)
    assert actual['exit_code'] == expected['exit_code'] == 0
    assert actual['compiler_backend']['mode'] == 'reused-jvm'
    assert not (root/'processor-ran').exists()
    assert snapshot() == classes and (root/'processor-api.txt').read_bytes() == diagnostic
    # Explicit activation is not emulated: the original CLI must execute it.
    active = processor_command.copy()
    active.insert(2, '-proc:full')
    expected = execute(active, changed, root/'active-cli.txt', 20)
    assert (root/'processor-ran').is_file() and expected['exit_code'] != 0
    (root/'processor-ran').unlink()
    actual = session.execute(active, changed, root/'active-api.txt', 20)
    assert actual['compiler_backend']['mode'] == 'subprocess'
    assert actual['exit_code'] == expected['exit_code']
    assert (root/'processor-ran').is_file()
    assert (root/'active-api.txt').read_bytes() == (root/'active-cli.txt').read_bytes()
    reset()
    recovered = session.execute(command, changed, root/'after-active.txt', 20)
    assert recovered['exit_code'] == 0 and recovered['compiler_backend']['cold']
    # Plugins may autoStart without -Xplugin, so even registration remains excluded.
    plugin = root/'SentinelPlugin.java'
    plugin.write_text('''
import com.sun.source.util.*;
public class SentinelPlugin implements Plugin {
  public String getName() { return "sentinel"; }
  public boolean autoStart() { return true; }
  public void init(JavacTask task, String... args) {
    try { java.nio.file.Files.writeString(java.nio.file.Path.of("/experiment/plugin-ran"), "ran"); }
    catch (java.io.IOException e) { throw new RuntimeException(e); }
    throw new IllegalStateException("automatic plugin activation");
  }
}
''')
    assert execute(['javac', '-d', str(extension_classes), str(plugin)], root, root/'plugin-build.txt', 20)['exit_code'] == 0
    plugin_jar = root/'plugin.jar'
    with zipfile.ZipFile(plugin_jar, 'w') as jar:
        jar.write(extension_classes/'SentinelPlugin.class', 'SentinelPlugin.class')
        jar.writestr('META-INF/services/com.sun.source.util.Plugin', 'SentinelPlugin\n')
    # The explicit request classpath overrides the environment on CLI and API alike;
    # source-launching our helper must not activate a plugin from ambient CLASSPATH.
    os.environ['CLASSPATH'] = str(plugin_jar)
    session.close()
    actual = session.execute(command, changed, root/'ambient-classpath.txt', 20)
    assert actual['exit_code'] == 0 and actual['compiler_backend']['mode'] == 'reused-jvm'
    assert not (root/'plugin-ran').exists()
    plugin_command = command.copy()
    plugin_command[3] += os.pathsep+str(plugin_jar)
    expected = execute(plugin_command, changed, root/'plugin-cli.txt', 20)
    assert expected['exit_code'] != 0 and (root/'plugin-ran').exists()
    (root/'plugin-ran').unlink()
    actual = session.execute(plugin_command, changed, root/'plugin-api.txt', 20)
    assert actual['compiler_backend']['mode'] == 'subprocess' and (root/'plugin-ran').exists()
    assert actual['exit_code'] == expected['exit_code']
    assert (root/'plugin-api.txt').read_bytes() == (root/'plugin-cli.txt').read_bytes()
    del os.environ['CLASSPATH']
    # A real compiler JVM timeout and an externally killed JVM cannot poison later work.
    session.close()
    timed = session.execute(command, changed, root/'timeout.txt', .001)
    assert timed['timeout'] and timed['compiler_backend']['discarded'] and session.child is None
    assert not list(output.iterdir())
    recovered = session.execute(command, changed, root/'after-timeout.txt', 20)
    assert recovered['exit_code'] == 0
    crashed = session.child
    crashed.kill()
    crashed.wait()
    recovered = session.execute(command, changed, root/'after-crash.txt', 20)
    assert recovered['exit_code'] == 0 and session.child.pid != crashed.pid
    final_child = session.child
assert final_child.poll() is not None
print(json.dumps(dict(equivalent=True, same_pid=len(set(pids)) == 1, restarted=True,
                     cache_miss_then_hit=True, cache_backend_separated=True)))
