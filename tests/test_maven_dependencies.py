import hashlib
from pathlib import Path

import pytest

from test_survival.adapters import java_worker as worker, maven_dependencies as dependencies
from test_survival.io import read_json, write_json
from test_survival.config import validate as validate_config


@pytest.mark.parametrize('cached', [False, True])
def test_resolve_recovers_volume_and_retries_429(tmp_path, cached):
    cache = tmp_path/'classpaths/pom.txt'
    jar = tmp_path/'m2/slf4j.jar'
    cache.parent.mkdir()
    if cached:
        cache.write_text('/missing/old-volume.jar')
    calls, sleeps = [], []
    def execute(command, cwd, log, timeout):
        calls.append(command)
        log.parent.mkdir(parents=True, exist_ok=True)
        goal = next(c for c in command if c.startswith('dependency:'))
        online_attempts = sum(goal in c and '-o' not in c for c in calls)
        fail = '-o' in command or online_attempts == 1
        log.write_text('HTTP 429 Too Many Requests' if fail else 'resolved')
        if not fail and goal == 'dependency:build-classpath':
            jar.parent.mkdir(exist_ok=True);jar.touch()
            Path(next(c.split('=', 1)[1] for c in command if c.startswith('-Dmdep.outputFile='))).write_text(str(jar))
        return {'exit_code': int(fail), 'log': str(log)}
    attempts = dependencies.resolve(tmp_path, cache, tmp_path/'logs', 60, execute, wait=sleeps.append)
    assert cache.read_text() == str(jar)
    assert len(attempts) == (3 if cached else 6)
    assert sleeps == [60]*(1 if cached else 2)
    assert dependencies.resolve(tmp_path, cache, tmp_path/'logs', 60, execute) == []
    assert all(Path(a['log']).exists() for a in attempts)


@pytest.mark.parametrize('failure', ['command', 'missing-jar'])
def test_failed_rebuild_never_publishes_classpath(tmp_path, failure):
    cache = tmp_path/'pom.txt';cache.write_text('/missing/original.jar')
    def execute(command, cwd, log, timeout):
        log.parent.mkdir(parents=True, exist_ok=True);log.write_text('invalid dependency')
        output = Path(next(c.split('=', 1)[1] for c in command if c.startswith('-Dmdep.outputFile=')))
        output.write_text('/missing/replacement.jar')
        return {'exit_code': int(failure == 'command')}
    with pytest.raises(RuntimeError):
        dependencies.resolve(tmp_path, cache, tmp_path/'logs', 60, execute)
    assert cache.read_text() == '/missing/original.jar'
    assert not list(tmp_path.glob('*.tmp'))


def test_setup_resolves_anchor_after_jars_and_upgrades_old_marker(tmp_path, monkeypatch):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.config import validate
    config = validate({'name': 'x', 'anchor': 't', 'project': {'repo': '/repo'}})
    adapter = JavaMavenAdapter(tmp_path, config)
    folder = adapter.folder(0);folder.mkdir(parents=True);(folder/'pom.xml').write_text('<project/>')
    helpers = tmp_path/'helpers';helpers.mkdir()
    payload = b'runner jar'
    records = [dict(filename='test.jar', sha256=hashlib.sha256(payload).hexdigest(), role='test')]
    write_json(helpers/'classpath-artifacts.json', records)
    adapter.helpers = helpers
    libs = tmp_path/'work/libs';libs.mkdir();(libs/'test.jar').write_bytes(payload)
    write_json(tmp_path/'results/runner-setup.json', {'status': 'completed', 'jars': records})
    for key, value in dict(EXP=tmp_path, RESULTS=tmp_path/'results', HELPERS=helpers, REPO=folder, TIMEOUTS={'build': 60}).items():
        monkeypatch.setattr(worker, key, value, raising=False)
    calls = []
    def execute(command, cwd, log, timeout):
        assert (libs/'test.jar').read_bytes() == payload
        assert cwd == folder
        calls.append(command)
        if 'dependency:build-classpath' in command:
            Path(next(c.split('=', 1)[1] for c in command if c.startswith('-Dmdep.outputFile='))).write_text('')
        return {'exit_code': 0}
    monkeypatch.setattr(worker, 'execute', execute)
    monkeypatch.setattr(adapter, 'worker', lambda job: worker.setup())
    adapter.setup_runner()
    assert calls[0] == ['mvn', '-q', '-o', 'dependency:go-offline']
    assert 'dependency:build-classpath' in calls[1]
    adapter.setup_runner()
    assert len(calls) == 2
    assert read_json(tmp_path/'results/runner-setup.json')['project_dependencies'] == hashlib.sha256(b'<project/>').hexdigest()


def test_build_records_unresolvable_dependencies_as_a_failed_build(tmp_path, monkeypatch):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.config import validate
    adapter = JavaMavenAdapter(tmp_path, validate({'name': 'x', 'anchor': 't', 'project': {'repo': '/repo'}}))
    monkeypatch.setattr(adapter, 'checkout', lambda index, sha: None)
    def unresolvable(folder, index, **kwargs):
        raise RuntimeError('Maven classpath resolution failed; see results/builds/logs/classpath-3.txt')
    monkeypatch.setattr(adapter.cache, 'resolve', unresolvable)
    monkeypatch.setattr(adapter.runner, 'execute', lambda *args: pytest.fail('compile must not run without a classpath'))
    result = adapter.build(3, 'abc')
    assert result['build'] == 'fail' and 'classpath-3.txt' in result['classpath_error']
    assert read_json(tmp_path/'results/builds/3.json') == result


def test_worker_checks_all_cached_entries_before_filtering_junit(tmp_path, monkeypatch):
    folder = tmp_path/'work/checkouts/0';folder.mkdir(parents=True)
    cache = tmp_path/'work/classpaths/pom.txt';cache.parent.mkdir()
    cache.write_text('/missing/org/junit/junit.jar:/missing/slf4j.jar')
    write_json(tmp_path/'results/builds/0.json', {'classpath': 'work/classpaths/pom.txt'})
    helpers = tmp_path/'helpers';helpers.mkdir();write_json(helpers/'classpath-artifacts.json', [])
    for key, value in dict(EXP=tmp_path, RESULTS=tmp_path/'results', HELPERS=helpers, MODULE='', TIMEOUTS={'build': 60}).items():
        monkeypatch.setattr(worker, key, value, raising=False)
    jar = tmp_path/'slf4j.jar';jar.touch()
    def execute(command, cwd, log, timeout):
        assert 'dependency:build-classpath' in command
        Path(next(c.split('=', 1)[1] for c in command if c.startswith('-Dmdep.outputFile='))).write_text(str(jar))
        return {'exit_code': 0}
    monkeypatch.setattr(worker, 'execute', execute)
    assert worker.classpath(0) == (folder, str(folder/'target/classes')+':'+str(jar))


def test_compile_tests_uses_the_worker_classpath_order(tmp_path, monkeypatch):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.config import validate
    adapter = JavaMavenAdapter(tmp_path, validate({'name': 'x', 'anchor': 't', 'project': {'repo': '/repo'}}))
    helpers = tmp_path/'helpers';helpers.mkdir()
    write_json(helpers/'classpath-artifacts.json', [dict(filename='junit-platform-console-standalone-1.9.3.jar', role='test'),
                                                    dict(filename='mockito-core-5.24.0.jar', role='test'),
                                                    dict(filename='org.jacoco.cli-0.8.14-nodeps.jar', role='coverage')])
    adapter.helpers = helpers
    (tmp_path/'work/libs').mkdir(parents=True);(tmp_path/'work/libs/byte-buddy-1.14.1.jar').touch()  # Stale, unlisted.
    cache = tmp_path/'work/classpaths/pom.txt';cache.parent.mkdir(parents=True)
    cache.write_text('/root/.m2/repository/org/junit/jupiter/junit-jupiter-api/5.8.2/junit-jupiter-api-5.8.2.jar:/root/.m2/repository/org/slf4j/slf4j-api/2.0.9/slf4j-api-2.0.9.jar\n')
    monkeypatch.setattr(adapter.cache, 'resolve', lambda folder, index: ('digest', cache, []))
    commands = []
    def execute(command, cwd, log, timeout):
        commands.append(command);return {'exit_code': 0}
    monkeypatch.setattr(adapter.runner, 'execute', execute)
    adapter.compile_tests(0, [tmp_path/'generated/A.java', tmp_path/'generated/B.java'])
    libs = adapter.runner.path(tmp_path/'work/libs')
    expected = ':'.join([adapter.runner.path(adapter.folder(0)/'target/classes'), libs+'/mockito-core-5.24.0.jar',
                         libs+'/junit-platform-console-standalone-1.9.3.jar', '/root/.m2/repository/org/slf4j/slf4j-api/2.0.9/slf4j-api-2.0.9.jar'])
    assert [c[c.index('-cp')+1] for c in commands] == [expected, expected]


@pytest.mark.parametrize('module, depth, selector', [('.', 0, []), ('', 0, []), ('./', 0, []),
                                                     ('implementation', 1, ['-pl', 'implementation', '-am']),
                                                     ('sources/yaml', 2, ['-pl', 'sources/yaml', '-am'])])
def test_reactor_runs_submodules_from_the_checkout_root(tmp_path, module, depth, selector):
    root = tmp_path/'work/checkouts/3'
    folder = root if depth == 0 else root/module
    assert dependencies.reactor(folder, module) == (root, selector)


def test_submodule_artifact_goals_package_the_reactor_first(tmp_path):
    folder = tmp_path/'implementation'
    args, cwd = dependencies.command('dependency:build-classpath', folder, 'implementation', offline=True, options=('-DincludeScope=test',))
    assert cwd == tmp_path
    assert args == ['mvn', '-q', '-o', '-pl', 'implementation', '-am', '-Dmaven.test.skip=true', 'package', 'dependency:build-classpath', '-DincludeScope=test']
    args, _ = dependencies.command('dependency:go-offline', folder, 'implementation', refresh=True)
    assert args == ['mvn', '-q', '-U', '-pl', 'implementation', '-am', 'dependency:go-offline']
    # A root module keeps the original command and working directory.
    assert dependencies.command('dependency:build-classpath', tmp_path, '.', offline=True) == (['mvn', '-q', '-o', 'dependency:build-classpath'], tmp_path)


def test_submodule_classpath_resolves_in_the_reactor_and_is_not_shared(tmp_path):
    from test_survival.runner.cache import ClasspathCache
    config = validate_config({'name': 'x', 'anchor': 't', 'project': {'repo': str(tmp_path), 'module': 'implementation'}})
    calls = []
    def execute(command, cwd, log, timeout):
        calls.append((command, cwd))
        if 'dependency:build-classpath' in command:
            sibling = Path(cwd)/'common/target/common-1.1-SNAPSHOT.jar'
            sibling.parent.mkdir(parents=True, exist_ok=True);sibling.touch()
            Path(next(c.split('=', 1)[1] for c in command if c.startswith('-Dmdep.outputFile='))).write_text(str(sibling))
        return {'exit_code': 0}
    class Runner:
        path = staticmethod(str)
        def execute(self, command, cwd, log, timeout):
            job = read_json(command[-1])
            attempts = dependencies.resolve(Path(job['folder']), tmp_path/job['cache'], tmp_path/'logs', 10, execute,
                                            force=job['force'], module=job['module'])
            write_json(tmp_path/job['output'], {'attempts': attempts})
            return {'exit_code': 0}
    cache = ClasspathCache(tmp_path, config, Runner())
    paths = []
    for index in (1, 2):
        root = tmp_path/f'work/checkouts/{index}'
        (root/'implementation').mkdir(parents=True);(root/'implementation/pom.xml').write_text('<project/>')
        paths.append(cache.resolve(root/'implementation', index)[1])
    # Identical module poms still get their own classpath: each names its own checkout's sibling jar.
    assert [p.name for p in paths] == ['1.txt', '2.txt']
    assert paths[0].read_text().startswith(str(tmp_path/'work/checkouts/1/common'))
    assert all(cwd.name in ('1', '2') and '-pl' in command for command, cwd in calls)


def test_submodule_build_compiles_from_the_reactor_root(tmp_path, monkeypatch):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    adapter = JavaMavenAdapter(tmp_path, validate_config({'name': 'x', 'anchor': 't', 'project': {'repo': '/repo', 'module': 'implementation'}}))
    monkeypatch.setattr(adapter, 'checkout', lambda index, sha: None)
    cache = tmp_path/'work/classpaths/3.txt';cache.parent.mkdir(parents=True);cache.write_text('')
    monkeypatch.setattr(adapter.cache, 'resolve', lambda folder, index, **kwargs: ('digest', cache, []))
    monkeypatch.setattr(adapter, 'dependency_identity', lambda *args: [])
    commands = []
    def execute(command, cwd, log, timeout):
        commands.append((command, Path(cwd)));return {'exit_code': 0}
    monkeypatch.setattr(adapter.runner, 'execute', execute)
    assert adapter.build(3, 'abc')['build'] == 'ok'
    assert commands == [(['mvn', '-q', '-o', '-pl', 'implementation', '-am', '-DskipTests', 'compile'], tmp_path/'work/checkouts/3')]
