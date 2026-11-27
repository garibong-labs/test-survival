from contextlib import nullcontext
import subprocess

import pytest

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.cli import main
from test_survival.config import STAGES, load
from test_survival.io import atomic_text, read_csv, read_json, write_csv, write_json
from test_survival.panel import panel_lines
from test_survival.runner.docker import DockerRunner


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Use real Git history and stages, replacing only external Java jobs."""
    repo = tmp_path/'repo'
    repo.mkdir()
    def git(*args):
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    git('init', '-b', 'main')
    git('config', 'user.email', 'fixture@example.com')
    git('config', 'user.name', 'Fixture')
    source = repo/'src/main/java/A.java'
    source.parent.mkdir(parents=True)
    source.write_text('class A {}')
    git('add', '.')
    git('commit', '-m', 'anchor')
    git('tag', 'anchor')
    source.write_text('class A { int i; }')
    git('commit', '-am', 'production change')
    (repo/'README').touch()
    git('add', '.')
    git('commit', '-m', 'untouched')

    events = []
    def build(adapter, index, sha):
        events.append((adapter.exp.name, index))
        record = dict(index=index, sha=sha, build='ok', input_sha256=adapter.build_fingerprint(sha))
        write_json(adapter.exp/f'results/builds/{index}.json', record)
        (adapter.folder(index)/'target/classes').mkdir(parents=True)
        return record
    def baseline(adapter):
        events.append((adapter.exp.name, 'baseline'))
        test = adapter.exp/'dev/src-test-t/java/ATest.java'
        test.parent.mkdir(parents=True)
        test.write_text('class ATest {}')
        row = {'class':'ATest', 'method':'works', 'kind':'plain', 'stable':'yes'}
        write_csv(adapter.exp/'dev/dev-methods-all.csv', [row], row)
        method = {'class':'A', 'method':'work', 'signature':'work()', 'line':1, 'eligible':True}
        write_json(adapter.exp/'results/source-inventory.json', {
            'main':dict(classes={'A':dict(path='src/main/java/A.java')}, methods=[method]),
            'test':dict(classes={}, methods=[])})
        return [row]
    def execute(runner, command, cwd, log, timeout):
        if any(str(arg).endswith('AccessorAudit.java') for arg in command):
            atomic_text(log, 'class\tmethod\taccessor\n')
        else:
            assert any(str(arg).endswith('RenderFigure.java') for arg in command)
        return dict(exit_code=0)
    def worker(adapter, job):
        index = job['index']
        events.append((adapter.exp.name, f'measure-{index}'))
        if index:
            assert (adapter.exp/'results/commits-check.md').is_file()
            assert not (adapter.exp/'state/range-builds.json').exists()
        lines = [['src/main/java/A.java', 1]]
        methods = [dict(t, state='pass', message='', flaky=False, lines=lines) for t in job['methods']]
        record = dict(methods=methods, status='evaluated', sha=job['sha'], files=[], runs=[], compile=[],
                      coverage_version=3, universe=lines, index=index)
        if job.get('phase') == 'B':
            write_csv(adapter.exp/f'results/coverage/{index}-dev-lines.csv',
                      [dict(file=lines[0][0], line=1, covered='yes')], ['file','line','covered'])
        write_json(adapter.exp/job['output'], record)
        return record
    monkeypatch.setattr(JavaMavenAdapter, 'build', build)
    monkeypatch.setattr(JavaMavenAdapter, 'baseline', baseline)
    monkeypatch.setattr(JavaMavenAdapter, 'setup_runner', lambda self: None)
    monkeypatch.setattr(JavaMavenAdapter, 'worker', worker)
    monkeypatch.setattr(DockerRunner, 'execute', execute)
    monkeypatch.setattr(DockerRunner, 'session', lambda *args: nullcontext())
    return repo, events


@pytest.mark.parametrize('mode', ['legacy', 'batch'])
def test_defer_resume_matches_full_run(tmp_path, project, mode, capsys):
    repo, events = project
    config = dict(name='fixture', project=dict(repo=str(repo)), anchor='anchor', range=dict(branch='main', skip_untouched='main-sources'),
                  scope=dict(rule='explicit', classes=['A'], min_classes=1), dev=dict(denominator='project'))
    if mode == 'batch':
        config['survival'] = dict(execution='batch')
    split, full = tmp_path/'split', tmp_path/'full'
    for exp in (split, full):
        write_json(exp/'experiment.yaml', config)
    split_args = ['run', str(split), '--no-panel']
    assert main([*split_args, '--until', 'screen']) == 0
    assert events == [('split', 0), ('split', 'baseline')]
    assert sorted(p.name for p in (split/'results/builds').iterdir()) == ['0.json']
    assert not (split/'results/commits-check.md').exists()
    assert read_json(split/'state/range-builds.json') == dict(status='deferred')
    assert main(['status', str(split)]) == 0
    display = capsys.readouterr().out
    assert 'range builds deferred' in display and '1/1 builds' in display and '/3 builds' not in display
    for width in (80, 160):
        panel = panel_lines(split, load(split), width)
        assert 'range builds deferred' in panel[0]
        assert all(len(line) < width for line in panel)
        assert all(stage in panel[1] for stage in STAGES)
    assert main([*split_args, '--until', 'control']) == 0
    anchor_states = {s:(split/f'state/{s}.json').read_bytes() for s in STAGES[:5]}
    assert main(split_args) == 0
    assert all((split/f'state/{s}.json').read_bytes() == data for s,data in anchor_states.items())
    assert events.index(('split', 'measure-0')) < events.index(('split', 1)) < events.index(('split', 'measure-1'))
    assert events.count(('split', 1)) == events.count(('split', 0)) == events.count(('split', 'baseline')) == 1
    assert main(['run', str(full), '--no-panel']) == 0
    assert events.index(('full', 1)) < events.index(('full', 'measure-0'))
    assert read_json(full/'results/builds/2.json')['build'] == 'skipped-untouched'
    for name in ('builds/0.json', 'builds/1.json', 'builds/2.json', 'commits-check.md', 'commits.csv',
                 'commit-paths.json', 'frozen-denominators.json', 'survival.csv', 'changed-lines.csv',
                 'survival-consistency.json', 'coverage-consistency.json', 'table1-survival-by-event.md'):
        assert (split/'results'/name).read_bytes() == (full/'results'/name).read_bytes(), name
    assert [r['evaluated'] for r in read_csv(split/'results/survival.csv')] == ['yes', 'yes', 'no']
    before = list(events)
    assert main(split_args) == 0
    assert main(['run', str(full), '--until', 'screen']) == 0
    assert events == before
    assert not (full/'state/range-builds.json').exists()


@pytest.mark.parametrize('deferred_directory', ['policy', 'baseline'])
def test_policy_commands_name_the_experiment_to_resume(tmp_path, capsys, deferred_directory):
    exp = tmp_path/'policy'
    directory = tmp_path/deferred_directory
    config = dict(name='fixture', anchor='t', project=dict(repo=str(tmp_path)),
                  policies=dict(baseline='baseline', groups=['dev'], set=[dict(name='reset')]))
    write_json(exp/'experiment.yaml', config)
    marker = directory/'state/range-builds.json'
    write_json(marker, dict(status='deferred'))
    for command in ('plan', 'run'):
        assert main(['policy', command, str(exp)]) == 2
        assert f'survival run {directory}' in capsys.readouterr().err
        assert read_json(marker) == dict(status='deferred')
        assert not (exp/'results/policy').exists()


def test_failed_range_build_retries_only_range_before_single_stage(tmp_path, project, monkeypatch):
    repo, events = project
    exp = tmp_path/'retry'
    write_json(exp/'experiment.yaml', dict(name='fixture', project=dict(repo=str(repo)), anchor='anchor',
               range=dict(branch='main', skip_untouched='main-sources'),
               scope=dict(rule='explicit', classes=['A'], min_classes=1), dev=dict(denominator='project')))
    args = ['run', str(exp), '--no-panel']
    assert main([*args, '--until', 'control']) == 0
    states = {s:(exp/f'state/{s}.json').read_bytes() for s in STAGES[:5]}

    def fail(adapter, index, sha):
        assert index == 1
        raise RuntimeError('build worker interrupted')

    with monkeypatch.context() as patch:
        patch.setattr(JavaMavenAdapter, 'build', fail)
        assert main([*args, '--stage', 'survive']) == 1
    assert read_json(exp/'state/range-builds.json')['status'] == 'failed'
    assert 'build worker interrupted' in (exp/'state/range-builds-error.txt').read_text()
    assert not (exp/'results/commits-check.md').exists()
    assert ('retry', 'measure-1') not in events
    assert main([*args, '--stage', 'survive']) == 0
    assert all((exp/f'state/{s}.json').read_bytes() == data for s,data in states.items())
    assert ('retry', 'measure-1') in events
