"""Run one resolved Maven fixture in the author classpath and batch worker."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

from test_survival.adapters import ktester_classpath as cp

profile = sys.argv[1]
exp = Path('/fixture')
runner = exp/'work/libs'
project = exp/'work/ktester/k/projects/sample'
project.mkdir(parents=True)
jars = Path('/libs')/profile
shutil.copytree(jars, project/'libs')
runner.symlink_to('/libs')
supplement = project/'target/ktester-test-libs'
package = Path(cp.__file__).parent


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


if profile == 'assertj':
    assert (project/'libs/byte-buddy-1.12.21.jar').is_file()
else:
    assert (project/'libs/mockito-core-5.19.0.jar').is_file()

record = cp.configure(project, runner, supplement, None)
assert cp.configure(project, runner, supplement, None) == record  # round 2
if profile == 'assertj':
    assert record['replaced'] == [dict(project_jar='byte-buddy-1.12.21.jar', runner_jar='byte-buddy-1.17.7.jar')]
    mockito, buddy = '5.24.0', '1.17.7'
else:
    assert record['added'] == ['junit-platform-console-standalone-1.9.3.jar'] and record['replaced'] == []
    mockito, buddy = '5.19.0', '1.17.6'
cp.smoke_test(project, supplement)

source = exp/'sources/MockitoTest.java'
source.parent.mkdir()
source.write_text('''
import org.junit.jupiter.api.Test;
import org.mockito.Mockito;
import net.bytebuddy.ByteBuddy;
import static org.junit.jupiter.api.Assertions.*;
public class MockitoTest {
    public static class Target { public int value() { return 1; } }
    @Test public void works() {
        assertTrue(Mockito.class.getProtectionDomain().getCodeSource().getLocation().getPath()
                   .endsWith("/mockito-core-MOCKITO.jar"));
        assertTrue(ByteBuddy.class.getProtectionDomain().getCodeSource().getLocation().getPath()
                   .endsWith("/byte-buddy-BUDDY.jar"));
        Target target = Mockito.mock(Target.class);
        Mockito.when(target.value()).thenReturn(9);
        assertEquals(9, target.value());
    }
}
'''.replace('MOCKITO', mockito).replace('BUDDY', buddy))
if Path('/author/code').is_dir():
    sys.path.insert(0, '/author/code')
    from tools.execute_test import JavaRunner
    author = JavaRunner(str(project), str(supplement))
    compiled, error = author.compile_test(str(source))
    assert compiled, error
    result = author.run_test_verbose('MockitoTest')
    assert '1 tests successful' in result, result
else:
    # Standalone CI image runs the same javac and ConsoleLauncher contract.
    subprocess.run(['javac', '-cp', '@dependencies.txt', '-d', 'target/test-classes', source], cwd=project, check=True)
    subprocess.run(['java', '-cp', f'libs/*:target/test-classes:target/classes:{supplement}/*',
                    'org.junit.platform.console.ConsoleLauncher', '--fail-if-no-tests', '--select-class', 'MockitoTest'],
                   cwd=project, check=True)

# Point the real batch worker at the original (unfiltered) Maven dependencies.
checkout = exp/'work/checkouts/0'
(checkout/'target/classes').mkdir(parents=True)
save(exp/'results/builds/0.json', {'classpath':'work/maven.txt'})
(exp/'work/maven.txt').write_text(':'.join(str(jar) for jar in sorted(jars.glob('*.jar'))))
method = dict(id='fixture', file='sources/MockitoTest.java', file_hash='fixture', source_root='sources',
              **{'class':'MockitoTest'}, method='works')
save(exp/'batch.json', dict(action='policy-survive', index=0, module='.', sha='fixture', experiment=str(exp),
                            methods=[method], scope_files=[], scope_classes=[], work='batch', output='batch-result.json',
                            jvm=dict(javac='256m', jacoco='256m', launcher='256m'),
                            timeouts=dict(build=60, compile=60, test_class=60)))
subprocess.run([sys.executable, package/'java_worker.py', exp/'batch.json'], check=True)
batch = json.loads((exp/'batch-result.json').read_text())
assert batch['methods'][0]['state'] == 'pass', batch
assert batch['runs'] and batch['runs'][0]['exit_code'] == 0, batch

if profile == 'assertj':
    # Reintroduce the bad pair as project-owned jars: setup must respect them,
    # then the smoke gate must reject the round before credentials or author code.
    for name in ('mockito-core', 'byte-buddy-agent', 'objenesis'):
        shutil.copy2(next(runner.glob(name+'-[0-9]*.jar')), project/'libs')
    (exp/'tool/code').mkdir(parents=True)
    save(exp/'round.json', dict(action='generate', experiment=str(exp), module='.', tool=str(exp/'tool'),
                                project_name='sample', group='k', round=1, retry=0, label='round1',
                                params=dict(temperature=0, fix_tries=1, workers=1), model='unused', env_key='NO_MODEL_KEY'))
    result = subprocess.run([sys.executable, package/'ktester_worker.py', exp/'round.json'], capture_output=True, text=True)
    failed = json.loads((exp/'results/k-round1.json').read_text())
    assert result.returncode and failed['model_calls'] == 0
    assert all(s in failed['preflight_error'] for s in ('Mockito smoke test failed', 'mockito-core-5.24.0.jar',
                                                        'byte-buddy-1.12.21.jar', 'PremainAttachAccess.getInstrumentation')), failed
    assert not (exp/'work/ktester/k/rounds/round1').exists()
