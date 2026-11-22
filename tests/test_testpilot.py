from collections import defaultdict
from hashlib import sha256
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from test_junit4 import java_environment
from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import ConfigError, STAGES, as_written, canonical_hash, stage_hash, validate
from test_survival.generators import testpilot_native as tp
from test_survival.generators.parts import context, import_hints, testpilot_materials as materials
from test_survival.io import atomic_text, read_json, write_csv, write_json
from test_survival.model.openrouter import OpenRouter
from test_survival.progress import generation_progress, items
from test_survival.stages.screen import screen_round


FIXTURES = Path(__file__).parent/'fixtures/testpilot'
ROW = {'class': 'p.Counter', 'method': 'add', 'line': '5', 'signature': 'add(int)'}
DECL = dict(header='public static int add(int x)',
            definition='public static int add(int x) {\n    return x + 1;\n}', doc='/** Add one. */')
USAGE = dict(file='README.md', line=2, call_line=2, text='Counter.add(2);')


def config(repo, params=None, **extra):
    return validate(dict(name='fixture', anchor='t', project={'repo': str(repo), 'test_framework': 'junit4'},
                         generators=[dict(name='tp', method='testpilot', impl='native', params=params or {})], **extra))


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=Fixture',
                                    '-c', 'user.email=fixture@example.invalid', *args], text=True).strip()


def commit(repo, message):
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', message)
    return git(repo, 'rev-parse', 'HEAD')


def test_config_and_existing_stage_hashes():
    group = config('/p')['generators'][0]
    assert group['params'] == dict(temperature=0, max_tokens=16384, completions=1, max_snippets=3, snippet_lines=20)
    resolved = as_written(group, include_defaults=True)
    assert 'import_hints' not in resolved['prompt']
    assert validate(dict(name='pool', anchor='t', project={'repo':'/p'}, generators=[resolved]))
    with pytest.raises(ConfigError, match='always includes import hints'):
        validate(dict(name='x', anchor='t', project={'repo':'/p'},
                      generators=[{**resolved, 'prompt':{'import_hints':False}}]))
    assert config('/p', {'workers': 4})['generators'][0]['params']['workers'] == 4
    with pytest.raises(ConfigError, match='not implemented'):
        validate(dict(name='x', project={'repo':'/p'}, anchor='t',
                      generators=[dict(name='tp', method='testpilot', impl='author-tool')]))
    for group, expected in [
        (dict(name='o', method='ouedraogo', impl='native'), '49daa07b6890f2fcdba60818027a7d63b862f010aa5d1351b88b2400960e151c'),
        (dict(name='k', method='ktester', tool_path='/k'), 'cd00bb78d8a8af83fc044fd2e5cee908fe4df7aa2e856b13da0a9632c1381fc6'),
    ]:
        c = validate(dict(name='hash', project={'repo':'/p'}, anchor='t', generators=[group]))
        assert canonical_hash([stage_hash(c, stage) for stage in STAGES]) == expected
    assert stage_hash(config('/p'), 'generate') != stage_hash(config('/p', screen={'stable_runs': 1}), 'generate')


def test_snippets_read_tracked_commit_and_preserve_matching_lines(tmp_path):
    git(tmp_path, 'init', '-q')
    atomic_text(tmp_path/'a.md', '```python\nadd(0)\n```\n~~~~java\nfirst\nsecond\nadd(1);\n~~~~\n'
                '```\nother.add(2);\n```\n')
    atomic_text(tmp_path/'examples/X.java', 'before\nCounter.add(3);\nafter\n')
    for folder in ('target', 'experiments/test', 'src/main'):
        atomic_text(tmp_path/folder/'demo.java', 'add(99);')
    atomic_text(tmp_path/'experiments/archive/README.md', '```java\nadd(99);\n```')
    sha = commit(tmp_path, 'anchor')
    atomic_text(tmp_path/'0.md', '```java\nadd(4);\n```')
    later = commit(tmp_path, 'later')
    atomic_text(tmp_path/'00.md', '```java\nadd(5);\n```')
    atomic_text(tmp_path/'a.md', 'changed working copy')
    old = materials.snippets(tmp_path, sha, ['add', 'add', 'dd'], 3, 2)
    assert [(s['file'], s['line'], s['call_line'], s['text']) for s in old['add']] == [
        ('a.md', 6, 7, 'second\nadd(1);'), ('a.md', 10, 10, 'other.add(2);'),
        ('examples/X.java', 1, 2, 'before\nCounter.add(3);')]
    assert old['dd'] == []
    assert materials.snippets(tmp_path, later, ['add'], 1, 20)['add'][0]['text'] == 'add(4);'


def inventory():
    return {}, {}, defaultdict(lambda: defaultdict(list)), {}


def test_golden_prompts():
    hints, _ = import_hints.render(inventory(), 'p.Counter', [], {'max_types': 15, 'max_lines': 400})
    row = {**ROW, **DECL, 'snippets': [USAGE]}
    assert tp.material_prompt(row, tp.OPTIONS, hints, 'Counter_add_1_SBDUTest') == (FIXTURES/'material.txt').read_text()
    retry = tp.retry_prompt('public class T { @Test public void check() {} }\n',
                            '/host/exp/generated/T.java:7: error\n/mounted-work/checkouts/0: missing symbol',
                            {'/host/exp':'', '/experiment':'', '/mounted-work':'work/'})
    assert retry == (FIXTURES/'retry.txt').read_text()


def round_fixture(exp, monkeypatch, *, doc=False, usage=False, completions=1, rows=None):
    c = config(exp, {'completions': completions})
    rows = rows or [ROW]
    write_csv(exp/'population/methods.csv', rows, list(ROW))
    write_csv(exp/'dev/dev-denominator.csv', [], ['id'])
    write_json(exp/'results/anchor.json', {'sha': 'fixture-sha'})
    monkeypatch.setattr(context, 'inventory', lambda *a, **kw: inventory())
    monkeypatch.setattr(materials, 'declarations', lambda a, d, rows: [
        {**DECL, 'doc': DECL['doc'] if doc else ''} for _ in rows])
    monkeypatch.setattr(materials, 'snippets', lambda *args: {'add': [USAGE] if usage else []})
    return c, tp.TestPilotNative(exp, c, c['generators'][0])


@pytest.mark.parametrize('doc,usage,count', [(False, False, 2), (True, False, 4), (True, True, 8)])
def test_material_prompt_counts_and_no_test(tmp_path, monkeypatch, doc, usage, count):
    cfg, gen = round_fixture(tmp_path, monkeypatch, doc=doc, usage=usage,
                             rows=[ROW, {**ROW, 'signature': 'add(String)', 'line': '6'}])
    def complete(prompt, params, directory):
        assert len(list(directory.parents[2].glob('prompts/Counter/Counter*.txt'))) == 2*count
        return {'status':'ok'}, 'No test.'
    monkeypatch.setattr(gen.client, 'complete', complete)
    result = gen.generate(1)
    assert len(result.calls) == 2*count and result.files == []
    summary = read_json(gen.directory/'refinement-summary.json')
    assert summary['no_test'] == count*2 and summary['retries_created'] == 0
    methods = [read_json(p) for p in (gen.directory/'refinements/Counter').glob('*.json')]
    assert len(methods) == 2
    assert all(m['method']['snippets'] == ([USAGE] if usage else []) for m in methods)
    assert generation_progress(tmp_path, cfg, {})['total'] == 2


def fake_transport(monkeypatch, respond):
    calls = []
    def complete(self, prompt, params, folder):
        assert not (folder/'request.json').exists(), 'Repeated model request'
        response = respond(folder, prompt)
        status = {'status':'ok'}
        calls.append(folder.name)
        write_json(folder/'request.json', self.request(prompt, params))
        atomic_text(folder/'response.md', response)
        write_json(folder/'run.json', status)
        return status, response
    monkeypatch.setattr(OpenRouter, 'complete', complete)
    return calls


def java_response(name, body):
    return (f'```java\npackage p;\nimport org.junit.Test;\nimport static org.junit.Assert.*;\n'
            f'public class {name} {{ @Test public void check() {{ {body} }} }}\n```\n')


def test_completions_retry_resume_and_verified_screen_reuse(tmp_path, monkeypatch):
    cfg, gen = round_fixture(tmp_path, monkeypatch, completions=2)
    calls = fake_transport(monkeypatch, lambda folder, prompt: java_response(
        re.search(r'public class (\w+)', prompt)[1], 'fail();' if 'R_c' not in folder.name else 'assertTrue(true);'))
    executions = []
    def worker(self, job):
        executions.append(job['file'])
        return dict(file=job['file'], compile={'exit_code':0}, methods=[dict(
            file=job['file'], state='pass' if '-retry/' in job['file'] and '/c1/' in job['file'] else 'fail',
            message='first error', body_hash='123', technique=job['technique'])])
    monkeypatch.setattr(JavaMavenAdapter, 'worker', worker)
    original = gen.execute
    def interrupt(record):
        original(record)
        if len(executions) == 5:
            raise RuntimeError('interrupted')
    monkeypatch.setattr(gen, 'execute', interrupt)
    with pytest.raises(RuntimeError, match='interrupted'):
        gen.generate(1)
    retry_prompts = {p:p.read_bytes() for p in gen.directory.glob('prompts/Counter/*R_c*.txt')}
    assert len(retry_prompts) == 4
    first_screen = next(path for path in gen.directory.glob('validation/*.json')
                        if '-retry/' not in read_json(path)['file'])
    saved_screen = read_json(first_screen)
    saved_screen['methods'][0]['message'] = 'different diagnostic after interruption'
    write_json(first_screen, saved_screen)
    monkeypatch.setattr(gen, 'execute', original)
    result = gen.generate(1)
    assert len(result.files) == len(set(executions)) == len(executions) == len(calls) == 8
    assert all(p.read_bytes() == old for p, old in retry_prompts.items())
    record = read_json(next(gen.directory.glob('refinements/*/*.json')))
    for parent in record['prompts'][:4]:
        retry, = [r for r in record['prompts'] if r['parent'] == parent['id']]
        assert parent['retry'] == retry['id'] and parent['outcome'] == 'fail'
        assert Path(parent['file']).name == Path(retry['file']).name and retry['retry'] is None
    summary = read_json(gen.directory/'refinement-summary.json')
    assert summary['prompts_by_label']['sig'] == 1 and summary['calls_by_label']['sig'] == 2
    assert len(set((tmp_path/r['prompt']).read_text() for r in record['prompts'][:2])) == 1
    _, funnel = screen_round(tmp_path, cfg, cfg['generators'][0], gen.directory)
    assert len(executions) == 8 and set(funnel['per_prompt']) == {'sig','sig-body','sig-retry','sig-body-retry'}
    assert generation_progress(tmp_path, cfg, {})['calls'] == 8
    assert len(items(tmp_path, 'generate', cfg, {})[0]) == 8
    gen.params['temperature'] = 1
    with pytest.raises(RuntimeError, match='different request parameters'):
        gen.generate(1)


def test_comment_only_variants_keep_strings_and_share_body_hash():
    def key(body):
        old = sha256(re.sub(r'\s', '', body).encode()).hexdigest()
        return tp.comment_free_bodies('class X { void check() {'+body+'} }', {old})[old]
    assert key('/* changed */ assertTrue(true);') == key('assertTrue(true); // second\n')
    assert key('f("//");') != key('f("");')
    block = 'f("""\n" // }\n""");'
    assert key(block) == key(block+'/* comment */')
    assert key(block) != key(block.replace('// }', '// x'))


def test_recover_published_worker_result_and_invalidate_changed_inputs(tmp_path, monkeypatch):
    cfg, gen = round_fixture(tmp_path, monkeypatch)
    directory = tmp_path/'generated/tp/round1'
    source = directory/'sources/Counter/sig/Counter_add_1_STest.java'
    atomic_text(source, 'public class Wrong {}')
    executions = []
    def interrupted_worker(self, job):
        executions.append(job['file'])
        result = dict(file=job['file'], compile={'exit_code':0}, parse={'exit_code':0}, methods=[])
        write_json(self.exp/job['output'], result)
        if len(executions) == 1:
            raise RuntimeError('host stopped after worker result')
        return result
    monkeypatch.setattr(JavaMavenAdapter, 'worker', interrupted_worker)
    screen = tp.CandidateScreen(gen.screen_adapter, directory, 'tp')
    with pytest.raises(RuntimeError, match='host stopped'):
        screen.run(source)
    assert tp.outcome(screen.run(source)) == ('absent', 'Method not reported by launcher', True)
    assert len(executions) == 1
    cfg['screen']['stable_runs'] += 1
    tp.CandidateScreen(gen.screen_adapter, directory, 'tp').run(source)
    assert len(executions) == 2
    assert len(list(directory.glob('validation-history/*/*.json'))) == 1


def test_transport_retry_and_unmodified_malformed_source(tmp_path, monkeypatch):
    _, gen = round_fixture(tmp_path, monkeypatch)
    gen.directory = tmp_path/'generated/tp/round1'
    candidate = tp.Candidate('Counter', 'Counter_add_1_', (), 1, False)
    gen.save_prompt(candidate, 'request')
    source = 'public class Wrong { @Test public void broken() {\r\n'
    response = '```java\r\n'+source+'```\r\n'
    attempts = []
    def complete(prompt, params, folder):
        attempts.append(folder.name)
        retry = folder.name == 'attempt-2'
        status = {'status':'ok' if retry else 'transport-error'}
        write_json(folder/'request.json', gen.client.request(prompt, params))
        write_json(folder/'run.json', status)
        atomic_text(folder/'response.md', response if retry else '')
        return status, response if retry else ''
    monkeypatch.setattr(gen.client, 'complete', complete)
    monkeypatch.setattr(tp.time, 'sleep', lambda seconds: None)
    for _ in range(2):
        record = gen.call(candidate, ROW)
        assert (tmp_path/record['file']).name == 'Counter_add_1_STest.java'
        assert (tmp_path/record['file']).read_bytes() == source.encode()
        assert len(record['calls']) == 2
    assert attempts == ['Counter_add_1_S', 'attempt-2']
    with pytest.raises(RuntimeError, match='Cannot change a prompt'):
        gen.save_prompt(candidate, 'modified request')


@pytest.fixture
def offline_java(monkeypatch, java_environment):
    from test_survival.runner.docker import DockerRunner
    command = DockerRunner.command
    def offline(self, args, cwd):
        result = command(self, args, cwd)
        assert result[:2] == ['docker', 'run']
        return [*result[:2], '--network', 'none', *result[2:]]
    monkeypatch.setattr(DockerRunner, 'command', offline)
    return java_environment


@pytest.mark.java_integration
def test_testpilot_source_materials(tmp_path, offline_java):
    image, _ = offline_java
    adapter = JavaMavenAdapter(tmp_path, config(tmp_path, runner={'docker':{'image':image}}))
    source = '''package p;
class Counter {
    /** Select a value. */
    @SuppressWarnings("unchecked")
    public static <T> T add(T value) throws Exception {
        return value;
    }
    int add(int value) { return value + 1; }
}'''
    atomic_text(adapter.folder(0)/'src/main/java/p/Counter.java', source)
    rows = [{**ROW, 'signature':'add(T)', 'line':'4'}, {**ROW, 'line':'8'}]
    first, second = materials.declarations(adapter, tmp_path, rows)
    assert first == dict(header='public static <T> T add(T value) throws Exception',
                         definition=source[source.index('@SuppressWarnings'):source.index('    int add')].rstrip(),
                         doc='/** Select a value. */')
    assert second == dict(header='int add(int value)', definition='int add(int value) { return value + 1; }', doc='')
    with pytest.raises(RuntimeError, match=r'Unmatched methods.csv row: p.Counter\s+123'):
        materials.declarations(adapter, tmp_path, [{**ROW, 'line':'123'}])


@pytest.mark.java_integration
def test_testpilot_junit4_policy_pool(tmp_path, offline_java, monkeypatch):
    """Generate and screen a pool at a later commit using the fixture's offline Maven classpath."""
    from test_survival.policy.pools import build_pool
    image, libs = offline_java
    repo, exp = tmp_path/'repo', tmp_path/'experiment'
    repo.mkdir()
    git(repo, 'init', '-q')
    atomic_text(repo/'pom.xml', '''<project><modelVersion>4.0.0</modelVersion>
<groupId>p</groupId><artifactId>counter</artifactId><version>1</version>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version>
<scope>test</scope></dependency></dependencies></project>''')
    source = repo/'src/main/java/p/Counter.java'
    atomic_text(source, 'package p;\npublic class Counter {\n public static int add(int x) { return x; }\n}')
    atomic_text(repo/'src/test/java/p/CounterTest.java',
                'package p; import org.junit.Test; public class CounterTest { @Test public void check() {} }')
    commit(repo, 'anchor')
    atomic_text(source, source.read_text().replace('return x;', 'return x + 1;'))
    atomic_text(repo/'docs/example.md', '```java\nCounter.add(7);\n```\n')
    point = commit(repo, 'new implementation and example')
    atomic_text(repo/'docs/example.md', 'working copy')
    cfg = config(repo, runner={'docker':{'image':image}}, screen={'stable_runs':1})
    write_json(exp/'state/run.json', {'status':'running', 'run_id':'fixture'})

    # The CI fixture image contains a JDK and verified libraries. Supply the
    # build result that prepare normally publishes without resolving dependencies.
    def fixture_build(self, index, sha):
        self.checkout(index, sha)
        root = self.folder(index)
        classes = root/'target/classes'
        classes.mkdir(parents=True)
        result = self.runner.execute(['javac', '-d', str(classes), str(root/'src/main/java/p/Counter.java')],
                                     root, self.exp/'build.log')
        assert result['exit_code'] == 0
        jars = self.exp/'work/libs'
        jars.mkdir()
        for jar in Path(libs).glob('*.jar'):
            shutil.copyfile(jar, jars/jar.name)
        atomic_text(self.exp/'work/cp.txt', '')
        build = dict(sha=sha, build='ok', classpath='work/cp.txt')
        write_json(self.exp/'results/builds/0.json', build)
        return build
    monkeypatch.setattr(JavaMavenAdapter, 'build', fixture_build)
    def respond(folder, prompt):
        name = re.search(r'public class (\w+)', prompt)[1]
        if folder.name.endswith('SBU'):
            return 'No test.'
        if folder.name.endswith('SB'):
            return java_response(name, 'missingMethod();')
        if folder.name.endswith(('SU', 'SUR')):
            return java_response(name, 'fail("fixture assertion");')
        body = 'assertEquals(2, Counter.add(1));' if folder.name.endswith('S') else 'assertEquals(3, Counter.add(2));'
        return java_response(name, body)
    calls = fake_transport(monkeypatch, respond)
    executions = []
    real_worker = JavaMavenAdapter.worker
    def observe(self, job):
        if job['action'] == 'screen':
            executions.append(job['file'])
        return real_worker(self, job)
    monkeypatch.setattr(JavaMavenAdapter, 'worker', observe)
    scope = {'classes':[{'class':'p.Counter', 'file':'src/main/java/p/Counter.java'}]}
    baseline = SimpleNamespace(exp=exp, execution_config=cfg, scope=scope, by_index={1:{'date':'fixture'}})
    kept = build_pool(baseline, {'group':'tp', 'index':1, 'sha':point}, 1)
    root = exp/'results/policy/pools/tp/1'
    directory = root/'generated/tp/round1'
    assert len(kept) == 2 and {r['unit'] for r in kept} == {'p.Counter#add(int)'}
    assert len(calls) == 6 and len(executions) == len(set(executions)) == 5
    method = read_json(next(directory.glob('refinements/*/*.json')))
    assert method['method']['snippets'][0]['text'] == 'Counter.add(7);'
    assert 'return x + 1;' in method['method']['definition']
    records = method['prompts']
    assert {r['technique']: r['outcome'] for r in records} == {
        'sig':'pass', 'sig-body':'compile-fail', 'sig-usage':'fail', 'sig-body-usage':'no-test',
        'sig-body-retry':'pass', 'sig-usage-retry':'fail'}
    for r in records:
        if r['file']:
            assert (root/r['file']).is_file() and read_json(root/r['validation'])['file'] == r['file']
    compile_retry = next(r for r in records if r['technique'] == 'sig-body-retry')
    prompt = (root/compile_retry['prompt']).read_text()
    assert 'cannot find symbol' in prompt and '/experiment/' not in prompt
    assert read_json(directory/'refinement-summary.json') == dict(
        methods=1, with_doc=0, with_snippets=1, prompts_by_label={r['technique']:1 for r in records},
        calls_by_label={r['technique']:1 for r in records}, tests_extracted=5, compiled=4, passed=2,
        retries_created=2, retries_passed=1, no_test=1, unique_tests=4)
    funnel = read_json(directory/'funnel.json')
    assert funnel['stable_passed'] == funnel['total_kept'] == 2
    assert funnel['per_prompt']['sig-body-usage']['files'] == 0
