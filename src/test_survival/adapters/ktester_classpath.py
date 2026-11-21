"""Select Maven and runner jars for KTester's javac and ConsoleLauncher."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

if __package__:
    from .mockito_classpath import MOCKITO_LIBRARIES, artifact, project_owns_mockito
else:
    from mockito_classpath import MOCKITO_LIBRARIES, artifact, project_owns_mockito

CONSOLE = 'junit-platform-console-standalone'


def platform_version(project):
    versions = {v for name, v in project.items() if name.startswith('junit-platform-')}
    if not versions:
        # Older project snapshots may contain Jupiter modules without a Platform jar.
        jupiter = {'junit-jupiter', 'junit-jupiter-api', 'junit-jupiter-engine', 'junit-jupiter-params',
                   'junit-vintage-engine'}
        versions = {'1.'+v[2:] if v.startswith('5.') else v for name, v in project.items() if name in jupiter}
    if len(versions) > 1:
        raise ValueError('Inconsistent project JUnit release versions: ' + ', '.join(sorted(versions)))
    return next(iter(versions), None)


def configure(project, runner_libs, supplement, resolve_console):
    project_libs = project/'libs'
    # libs/* in the author runtime is non-recursive. Keep displaced originals in
    # the disposable Maven snapshot so retries and later rounds see the same inputs.
    displaced = project_libs/'ktester-displaced'
    project_jars = {jar.name: jar for directory in (displaced, project_libs)
                    for jar in sorted(directory.glob('*.jar'))}
    supplied = {}
    for jar in project_jars.values():
        name, version = artifact(jar)
        if name:
            if name.startswith('junit-') and name in supplied and supplied[name] != version:
                raise ValueError(f'Conflicting project versions of {name}: {supplied[name]} and {version}')
            supplied[name] = version
    manifest = json.loads(Path(__file__).with_name('java').joinpath('classpath-artifacts.json').read_text())
    runner = {r['coordinate'].split(':')[1]: runner_libs/r['filename'] for r in manifest}
    version = platform_version(supplied) or runner[CONSOLE].stem.removeprefix(CONSOLE+'-')
    added = []
    required = {'junit-platform-console', 'junit-platform-launcher', 'junit-platform-engine',
                'junit-platform-commons', 'junit-jupiter-api', 'junit-jupiter-params', 'junit-jupiter-engine'}
    if CONSOLE not in supplied and not required.issubset(supplied):
        console = runner_libs/f'{CONSOLE}-{version}.jar'
        if not console.is_file():
            resolve_console(version, console)
        added.append(console)

    supply_mockito = not project_owns_mockito(supplied)
    if supply_mockito:
        added.extend(runner[name] for name in MOCKITO_LIBRARIES if runner[name].name not in project_jars)
    supplement.mkdir(parents=True, exist_ok=True)
    names = {jar.name for jar in added}
    for old in supplement.glob('*.jar'):
        if old.name not in names:
            old.unlink()
    for jar in added:
        shutil.copy2(jar, supplement/jar.name)

    replaced = []
    for jar in sorted(project_jars.values(), key=lambda p: p.name):
        name, _ = artifact(jar)
        # byte-buddy-dep exposes the same net.bytebuddy classes using external ASM.
        if name == 'byte-buddy-dep':
            name = 'byte-buddy'
        if supply_mockito and name in MOCKITO_LIBRARIES and jar.name != runner[name].name:
            displaced.mkdir(exist_ok=True)
            destination = displaced/jar.name
            replaced.append(dict(project_jar=jar.name, runner_jar=runner[name].name))
        else:
            destination = project_libs/jar.name
        if jar != destination:
            jar.replace(destination)
    entries = [os.path.relpath(jar, project) for jar in sorted(project_libs.glob('*.jar'))]
    entries.extend(os.path.relpath(supplement/jar.name, project) for jar in added)
    (project/'dependencies.txt').write_text(os.pathsep.join(['target/classes', *entries]))
    return dict(project=project.name, platform_version=version, added=[jar.name for jar in added], replaced=replaced)


def smoke_test(project, supplement, *, env=None):
    """Compile with the author's argument file and start its actual wildcard classpath."""
    runtime = os.pathsep.join(['libs/*', 'target/test-classes', 'target/classes', str(supplement/'*')])
    with TemporaryDirectory(dir=supplement.parent) as scratch:
        source = Path(scratch)/'KTesterMockitoSmoke.java'
        source.write_text('public class KTesterMockitoSmoke { public static void main(String[] args) {'
                          ' org.mockito.Mockito.mock(KTesterMockitoSmoke.class); } }')
        for command in (['javac', '-cp', '@dependencies.txt', '-d', scratch, str(source)],
                        ['java', '-cp', scratch+os.pathsep+runtime, source.stem]):
            try:
                subprocess.run(command, cwd=project, capture_output=True, text=True, errors='replace',
                               timeout=120, check=True, env=env)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                jars = [str(p.relative_to(project)) for directory in (project/'libs', supplement)
                        for p in sorted(directory.glob('*.jar')) if artifact(p)[0] in (*MOCKITO_LIBRARIES, 'mockito-all', 'byte-buddy-dep')]
                detail = getattr(exc, 'stderr', None) or str(exc)
                raise RuntimeError(f'KTester Mockito smoke test failed ({command[0]}). '
                                   f'Active jars: {", ".join(jars)}\n{detail}') from exc
