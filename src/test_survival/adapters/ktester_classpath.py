"""Project-first classpath for KTester's javac and JUnit ConsoleLauncher."""
import json
import os
from pathlib import Path
import re
import shutil

MOCKITO_LIBRARIES = ('mockito-core', 'mockito-junit-jupiter', 'byte-buddy', 'byte-buddy-agent', 'objenesis')
CONSOLE = 'junit-platform-console-standalone'


def artifacts(directory):
    found = {}
    for jar in sorted(directory.glob('*.jar')):
        match = re.fullmatch(r'(.+?)-(\d[^/]*)\.jar', jar.name)
        if match and not match[2].endswith(('-tests', '-sources', '-javadoc')):
            name, version = match.groups()
            if name.startswith('junit-') and name in found and found[name] != version:
                raise ValueError(f'Conflicting project versions of {name}: {found[name]} and {version}')
            found[name] = version
    return found


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
    """Keep Maven jars first on both paths. The standalone includes Jupiter API, params and engines."""
    project_libs = project/'libs'
    supplied = artifacts(project_libs)
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
    for name in MOCKITO_LIBRARIES:
        if name not in supplied:
            added.append(runner[name])

    supplement.mkdir(parents=True, exist_ok=True)
    names = {jar.name for jar in added}
    for old in supplement.glob('*.jar'):
        if old.name not in names:
            old.unlink()
    for jar in added:
        shutil.copy2(jar, supplement/jar.name)
    project_jars = sorted(project_libs.glob('*.jar'))
    entries = [os.path.relpath(jar, project) for jar in project_jars]
    entries.extend(os.path.relpath(supplement/jar.name, project) for jar in added)
    (project/'dependencies.txt').write_text(os.pathsep.join(['target/classes', *entries]))
    return dict(project=project.name, platform_version=version, added=[jar.name for jar in added])
