import subprocess

import pytest

from test_survival.config import validate
from test_survival.stages.prepare import run


@pytest.mark.parametrize('module,changed', [('.', False), ('module', False), ('module', True)])
def test_off_first_parent_anchor_names_first_containing_commit_and_compares_module(tmp_path, module, changed):
    repo = tmp_path/'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.PIPE).strip()
    git('init', '-b', 'main')
    git('config', 'user.email', 'test@example.com')
    git('config', 'user.name', 'Test')
    (repo/'module').mkdir()
    (repo/'module/source').write_text('base')
    git('add', '.')
    git('commit', '-m', 'base')
    git('switch', '-c', 'side')
    (repo/'module/source').write_text('anchor')
    git('commit', '-am', 'anchor')
    anchor = git('rev-parse', 'HEAD')
    git('switch', 'main')
    if changed:
        (repo/'module/other').write_text('different module tree')
    else:
        (repo/'README').write_text('outside module')
    git('add', '.')
    git('commit', '-m', 'main change')
    git('merge', '--no-ff', 'side', '-m', 'first containing commit')
    first = git('rev-parse', 'HEAD')
    git('commit', '--allow-empty', '-m', 'later commit')
    config = validate({'name': 'anchor', 'project': {'repo': str(repo), 'module': module},
                       'anchor': anchor, 'range': {'end': 'HEAD'}})
    with pytest.raises(RuntimeError) as error:
        run(tmp_path/'experiment', config)
    text = str(error.value)
    assert f'first first-parent commit containing anchor: {first}' in text
    assert f'set anchor to {first}' in text
    identical = module != '.' and not changed
    assert f'module tree identical to anchor: {"yes" if identical else "no"}' in text
    config['range']['end'] = git('rev-parse', first+'^1')
    with pytest.raises(RuntimeError, match='no first-parent commit at this endpoint contains the anchor'):
        run(tmp_path/'experiment', config)


@pytest.mark.parametrize('setup_fails', [False, True])
def test_runner_setup_finishes_before_parallel_builds(tmp_path, monkeypatch, setup_fails):
    from test_survival.stages import prepare
    events = []
    class Adapter:
        def __init__(self, *args): pass
        def git(self, *args):
            if args[0] == 'rev-parse': return args[1].removesuffix('^{commit}')
            if args[:2] == ('rev-list', '--first-parent'):
                return 'end' if '--reverse' in args else 'end\nanchor'
            if args[0] == 'diff': return 'src/main/java/A.java'
            if args[0] == 'show': return '2026-01-01\ncommit' if '%n' in args[2] else '2026-01-01'
            raise AssertionError(args)
        def build(self, index, sha):
            events.append(f'build-{index}')
            return {'build': 'ok'}
        def baseline(self): return []
        def setup_runner(self):
            events.append('setup')
            if setup_fails: raise RuntimeError('setup failed')
    class Pool:
        def __init__(self, **kwargs): events.append('pool')
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def map(self, fn, rows): return map(fn, rows)
    monkeypatch.setattr(prepare, 'JavaMavenAdapter', Adapter)
    monkeypatch.setattr(prepare, 'ThreadPoolExecutor', Pool)
    monkeypatch.setattr(prepare, 'log', lambda *args: None)
    config = validate({'name': 'order', 'anchor': 'anchor', 'project': {'repo': '/repo'}, 'range': {'end': 'end'}})
    if setup_fails:
        with pytest.raises(RuntimeError, match='setup failed'):
            prepare.run(tmp_path, config)
        assert events == ['build-0', 'setup']
    else:
        prepare.run(tmp_path, config)
        assert events == ['build-0', 'setup', 'pool', 'build-1']
