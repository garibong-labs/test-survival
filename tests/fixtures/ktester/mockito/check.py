"""Run one resolved Maven fixture in the author classpath and batch worker."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

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
    assert (project/'libs/mockito-junit-jupiter-4.11.0.jar').is_file()

record = cp.configure(project, runner, supplement, None)
assert cp.configure(project, runner, supplement, None) == record  # round 2
replacements = {'byte-buddy-1.12.21.jar': 'byte-buddy-1.17.7.jar'}
if profile == 'project-mockito':
    replacements.update({
        'mockito-core-4.11.0.jar': 'mockito-core-5.24.0.jar',
        'mockito-junit-jupiter-4.11.0.jar': 'mockito-junit-jupiter-5.24.0.jar',
        'byte-buddy-agent-1.12.19.jar': 'byte-buddy-agent-1.17.7.jar',
        'objenesis-3.3.jar': 'objenesis-3.3.jar',
    })
assert {r['project_jar']: r['runner_jar'] for r in record['replaced']} == replacements
cp.smoke_test(project, supplement)

source = exp/'sources/MockitoTest.java'
source.parent.mkdir()
source.write_text('''
import org.junit.jupiter.api.Test;
import org.mockito.MockedConstruction;
import org.mockito.MockedStatic;
import org.mockito.Mockito;
import net.bytebuddy.ByteBuddy;
import static org.junit.jupiter.api.Assertions.*;
public class MockitoTest {
    public static class Target {
        public int value() { return 1; }
        public static int staticValue() { return 2; }
    }
    @Test public void works() {
        assertTrue(Mockito.class.getProtectionDomain().getCodeSource().getLocation().getPath()
                   .endsWith("/mockito-core-5.24.0.jar"));
        assertTrue(ByteBuddy.class.getProtectionDomain().getCodeSource().getLocation().getPath()
                   .endsWith("/byte-buddy-1.17.7.jar"));
        Target target = Mockito.mock(Target.class);
        Mockito.when(target.value()).thenReturn(9);
        assertEquals(9, target.value());
        try (MockedStatic<Target> mocked = Mockito.mockStatic(Target.class)) {
            mocked.when(Target::staticValue).thenReturn(7);
            assertEquals(7, Target.staticValue());
        }
        try (MockedConstruction<Target> mocked = Mockito.mockConstruction(Target.class)) {
            Target instance = new Target();
            assertSame(instance, mocked.constructed().get(0));
        }
    }
}
''')
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
index = 0 if profile == 'assertj' else 113
checkout = exp/f'work/checkouts/{index}'
(checkout/'target/classes').mkdir(parents=True)
save(exp/f'results/builds/{index}.json', {'classpath':'work/maven.txt'})
(exp/'work/maven.txt').write_text(':'.join(str(jar) for jar in sorted(jars.glob('*.jar'))))
method = dict(id='fixture', file='sources/MockitoTest.java', file_hash='fixture', source_root='sources',
              **{'class':'MockitoTest'}, method='works')
save(exp/'batch.json', dict(action='policy-survive', index=index, module='.', sha='fixture', experiment=str(exp),
                            methods=[method], scope_files=[], scope_classes=[], work='batch', output='batch-result.json',
                            jvm=dict(javac='256m', jacoco='256m', launcher='256m'),
                            timeouts=dict(build=60, compile=60, test_class=60)))
subprocess.run([sys.executable, package/'java_worker.py', exp/'batch.json'], check=True)
batch = json.loads((exp/'batch-result.json').read_text())
assert batch['methods'][0]['state'] == 'pass', batch
assert batch['runs'] and batch['runs'][0]['exit_code'] == 0, batch

if profile == 'assertj':
    # Inject the original wildcard shadow after selection so the real smoke
    # gate sees broken jars even though the resolver now displaces them.
    from test_survival.adapters import ktester_worker
    configure = cp.configure

    def broken_classpath(*args):
        receipt = configure(*args)
        shutil.copy2(jars/'byte-buddy-1.12.21.jar', project/'libs')
        return receipt

    (exp/'tool/code').mkdir(parents=True)
    save(exp/'round.json', dict(action='generate', experiment=str(exp), module='.', tool=str(exp/'tool'),
                                project_name='sample', group='k', round=1, retry=0, label='round1',
                                params=dict(temperature=0, fix_tries=1, workers=1), model='unused', env_key='NO_MODEL_KEY'))
    with patch.object(cp, 'configure', broken_classpath), patch.object(sys, 'argv', ['ktester_worker', str(exp/'round.json')]):
        try:
            ktester_worker.main()
        except RuntimeError as exc:
            assert 'Mockito smoke test failed' in str(exc)
        else:
            raise AssertionError('Generation proceeded with a conflicting Byte Buddy')
    failed = json.loads((exp/'results/k-round1.json').read_text())
    assert failed['model_calls'] == 0
    assert all(s in failed['preflight_error'] for s in ('Mockito smoke test failed', 'mockito-core-5.24.0.jar',
                                                        'byte-buddy-1.12.21.jar', 'PremainAttachAccess.getInstrumentation')), failed
    assert not (exp/'work/ktester/k/rounds/round1').exists()
