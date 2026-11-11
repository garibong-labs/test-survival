import csv
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import zipfile
from contextlib import ExitStack

import pytest

from test_survival import archive as module
from test_survival.cli import main
from test_survival.display import load_display_settings, settings_path
from test_survival.io import write_json


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def put(exp, name, data='evidence'):
    path = exp/name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return path


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path/'config'))
    repo = tmp_path/'fork'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.name', 'Archive test')
    git(repo, 'config', 'user.email', 'archive@example.invalid')
    git(repo, 'remote', 'add', 'origin', 'https://example.invalid/fork.git')
    exp = repo/'experiments/finished'
    put(exp, 'docs/report.md', 'tracked report')
    put(repo, '.gitignore', 'experiments/finished/results/\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'fixture')
    put(exp, 'experiment.yaml', 'not needed for archive or archived status')
    put(exp, 'results/evidence.json', '{"answer": 42}')
    put(exp, 'generated/model.txt', 'paid output')
    put(exp, 'state/report.json', '{"status": "completed"}')
    put(exp, 'state/run.json', '{"status": "completed"}')
    put(exp, 'state/run.lock', '')
    dest = tmp_path/'Drive'
    dest.mkdir()
    return repo, exp, dest


def members(path):
    with zipfile.ZipFile(path) as bundle:
        return set(bundle.namelist())


def test_inclusion_manifest_and_index(experiment):
    repo, exp, dest = experiment
    put(exp, 'docs/handover.md')
    put(exp, 'dev/baseline.json')
    put(exp, 'population/sample.csv')
    put(exp, 'generated/target/valuable.txt')
    final = module.archive(exp, destination=dest)
    prefix = 'experiments/finished/'
    with zipfile.ZipFile(final) as bundle:
        assert bundle.testzip() is None
        manifest = json.loads(bundle.read('ARCHIVE-MANIFEST.json'))
        assert prefix+'generated/target/valuable.txt' in bundle.namelist()
        payload = [entry for entry in bundle.infolist() if entry.filename != 'ARCHIVE-MANIFEST.json']
        assert manifest['included_files'] == sum(not entry.is_dir() for entry in payload)
        assert manifest['included_directories'] == sum(entry.is_dir() for entry in payload)
        assert manifest['included_bytes'] == sum(entry.file_size for entry in payload)
        assert all(entry.compress_type == zipfile.ZIP_DEFLATED for entry in payload if not entry.is_dir())
    assert manifest['tool_commit']
    assert manifest['exp_dir'] == str(exp.resolve())
    assert manifest['remote_url'] == 'https://example.invalid/fork.git'
    assert manifest['branch'] == 'main'
    assert manifest['head_sha'] == git(repo, 'rev-parse', 'HEAD')
    assert manifest['dirty'] is True
    assert manifest['archived_utc'].endswith('+00:00')
    assert manifest['stage_states']['report.json']['status'] == 'completed'
    assert manifest['top_level_entries']['generated']['included_files'] == 2
    sidecar = json.loads(final.with_suffix('.manifest.json').read_text())
    digest = sidecar.pop('sha256')
    assert sidecar == manifest
    assert digest == module.sha256(final)
    with (dest/'archive-index.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]['sha256'] == digest
    assert dest/rows[0]['zip_relative_path'] == final
    assert not list(repo.parent.glob('.fork-archive-*'))


@pytest.mark.parametrize('include_work', [False, True])
def test_exclusion_rules_and_pruned_walk(experiment, monkeypatch, include_work):
    _, exp, dest = experiment
    excluded = []
    for name in module.REGENERABLE:
        excluded.append(put(exp, f'docs/{name}/excluded.txt').parent)
    excluded += [put(exp, 'work/phase-A/scratch.txt').parents[1],
                 put(exp, 'results/policy/pools/group/42/work/scratch.txt').parent]
    put(exp, 'results/policy/pools/group/42/work/compile-cache/cache.txt')
    put(exp, 'work/checkouts/repo/file.txt')
    put(exp, 'docs/.DS_Store')
    put(exp, 'docs/work/keep.txt')
    put(exp, 'results/policy/pools/group/42/generated/keep.txt')
    forbidden = {p for p in excluded if not (include_work and p.name == 'work')}
    scandir = os.scandir
    def guarded(path):
        assert Path(path) not in forbidden, f'Walk entered excluded tree: {path}'
        return scandir(path)
    monkeypatch.setattr(module.os, 'scandir', guarded)
    final = module.archive(exp, destination=dest, include_work=include_work)
    names = members(final)
    assert not any('excluded.txt' in name or '.DS_Store' in name for name in names)
    assert ('experiments/finished/work/phase-A/scratch.txt' in names) == include_work
    assert ('experiments/finished/results/policy/pools/group/42/work/scratch.txt' in names) == include_work
    assert not any('/compile-cache/' in name or '/checkouts/' in name for name in names)
    assert 'experiments/finished/docs/work/keep.txt' in names


@pytest.mark.parametrize('lock_name', ['run.lock', 'policy-run.lock', 'policy/run.lock'])
@pytest.mark.parametrize('dry_run', [False, True])
def test_refuse_locked(experiment, lock_name, dry_run):
    _, exp, dest = experiment
    path = put(exp, 'state/'+lock_name, '')
    with path.open('r') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(module.ArchiveError, match='locked'):
            module.archive(exp, destination=dest, prune=True, yes=True, dry_run=dry_run)
    assert (exp/'results/evidence.json').exists()
    assert not list(dest.iterdir())


def test_running_status_refused_even_with_unheld_lock(experiment):
    _, exp, dest = experiment
    put(exp, 'state/run.json', '{"status": "running", "pid": 99999999}')
    assert main(['archive', str(exp), '--dest', str(dest)]) == 2
    assert not list(dest.iterdir())


def test_missing_destination_and_settings_override(experiment, capsys):
    _, exp, dest = experiment
    assert main(['archive', str(exp), '--dry-run']) == 2
    assert 'No archive destination configured' in capsys.readouterr().err
    path = settings_path()
    path.parent.mkdir(parents=True)
    path.write_text('[display]\ntimezone = "UTC"\n[archive]\ndestination = '+json.dumps(str(dest)))
    assert load_display_settings().timezone.key == 'UTC'
    assert main(['archive', str(exp), '--dry-run']) == 0
    path.write_text('[archive]\ndestination = "/missing/default"')
    assert main(['archive', str(exp), '--dest', str(dest), '--dry-run']) == 0
    assert not list(dest.iterdir())


@pytest.mark.parametrize('value', ['false', '42', '""', '[]'])
def test_invalid_archive_settings(experiment, value):
    _, exp, _ = experiment
    path = settings_path()
    path.parent.mkdir(parents=True)
    path.write_text('[archive]\ndestination = '+value)
    assert main(['archive', str(exp), '--dry-run']) == 2


def test_no_overwrite_including_partial_and_sidecar(experiment):
    _, exp, dest = experiment
    first = module.archive(exp, destination=dest)
    original = first.read_bytes()
    second_name = first.with_name(first.stem+'-2.zip')
    second_name.with_name(second_name.name+'.partial').write_text('reserved')
    third_name = first.with_name(first.stem+'-3.zip')
    third_name.with_suffix('.manifest.json').write_text('reserved')
    fourth = module.archive(exp, destination=dest)
    assert fourth.name == first.stem+'-4.zip'
    assert first.read_bytes() == original
    assert second_name.with_name(second_name.name+'.partial').read_text() == 'reserved'
    assert third_name.with_suffix('.manifest.json').read_text() == 'reserved'


def test_sha_failure_blocks_prune(experiment, monkeypatch):
    _, exp, dest = experiment
    real_sha = module.sha256
    monkeypatch.setattr(module, 'sha256', lambda path: 'bad' if path.is_relative_to(dest) else real_sha(path))
    with pytest.raises(module.ArchiveError, match='SHA-256'):
        module.archive(exp, destination=dest, prune=True, yes=True)
    assert (exp/'results/evidence.json').exists()
    assert not (exp/'ARCHIVED.json').exists()
    assert not (dest/'archive-index.csv').exists()


def test_crc_failure_blocks_copy_and_prune(experiment, monkeypatch):
    _, exp, dest = experiment
    monkeypatch.setattr(zipfile.ZipFile, 'testzip', lambda self: 'bad-entry')
    with pytest.raises(module.ArchiveError, match='CRC'):
        module.archive(exp, destination=dest, prune=True, yes=True)
    assert (exp/'results/evidence.json').exists()
    assert not list(dest.iterdir())


def test_prune_preserves_tracked_readonly_nested_git_and_symlinks(experiment, tmp_path, capsys):
    repo, exp, dest = experiment
    tracked = put(exp, 'work/tracked.txt', 'keep')
    git(repo, 'add', '-f', str(tracked))
    readonly = put(exp, 'work/nested/.git/objects/read-only', 'loose object')
    readonly.chmod(0o444)
    readonly.parent.chmod(0o555)
    outside = put(tmp_path, 'outside/evidence.txt', 'outside')
    (exp/'outside-link').symlink_to(outside.parent, target_is_directory=True)
    final = module.archive(exp, destination=dest, prune=True, yes=True)
    remaining = {p.relative_to(exp).as_posix() for p in exp.rglob('*') if p.is_file()}
    assert remaining == {'docs/report.md', 'work/tracked.txt', 'state/run.lock', 'ARCHIVED.json'}
    assert tracked.read_text() == 'keep'
    assert outside.read_text() == 'outside'
    record = json.loads((exp/'ARCHIVED.json').read_text())
    assert record['destination'] == str(final)
    assert record['sha256'] == module.sha256(final)
    assert 'Bytes freed:' in capsys.readouterr().out
    assert main(['status', str(exp)]) == 0
    assert capsys.readouterr().out.strip() == f'archived to {final}'
    assert main(['status', str(exp), '--watch']) == 0
    assert capsys.readouterr().out.strip() == f'archived to {final}'
    with zipfile.ZipFile(final) as bundle:
        info = bundle.getinfo('experiments/finished/outside-link')
        assert stat.S_ISLNK(info.external_attr >> 16)
        assert bundle.read(info).decode() == str(outside.parent)


@pytest.mark.parametrize('answer, removed', [('wrong', False), ('finished', True)])
def test_prune_confirmation(experiment, monkeypatch, answer, removed):
    _, exp, dest = experiment
    def confirm(prompt):
        assert prompt == 'Type the experiment folder name to delete local files: '
        return answer
    monkeypatch.setattr('builtins.input', confirm)
    module.archive(exp, destination=dest, prune=True)
    assert (exp/'results/evidence.json').exists() != removed


def test_change_after_copy_blocks_prune(experiment, monkeypatch):
    _, exp, dest = experiment
    original = module.copy_verified
    def copy(*args):
        result = original(*args)
        put(exp, 'generated/new-output.txt', 'new paid output')
        return result
    monkeypatch.setattr(module, 'copy_verified', copy)
    with pytest.raises(module.ArchiveError, match='changed'):
        module.archive(exp, destination=dest, prune=True, yes=True)
    assert (exp/'generated/new-output.txt').exists()
    assert (exp/'results/evidence.json').exists()


def test_dry_run_exact_counts_and_no_writes(experiment, monkeypatch, capsys):
    repo, exp, dest = experiment
    put(exp, 'work/large-cache.txt', 'x'*100)
    (exp/'state/run.lock').unlink()
    before = {p: p.lstat() for p in repo.parent.rglob('*')}
    assert main(['archive', str(exp), '--dest', str(dest), '--dry-run', '--prune', '--yes']) == 0
    after = {p: p.lstat() for p in repo.parent.rglob('*')}
    assert before.keys() == after.keys()
    assert all(before[p].st_mtime_ns == after[p].st_mtime_ns for p in before)
    output = capsys.readouterr().out
    assert 'work\t0\t0\t1\t100\texcluded as regenerable; prune untracked' in output
    assert 'No files written or deleted.' in output


def test_destination_inside_experiment_refused(experiment):
    _, exp, _ = experiment
    with pytest.raises(module.ArchiveError, match='outside EXP_DIR'):
        module.archive(exp, destination=exp/'results', prune=True, yes=True)


def test_tracked_marker_refuses_prune(experiment):
    repo, exp, dest = experiment
    marker = put(exp, 'ARCHIVED.json', '{}')
    git(repo, 'add', str(marker))
    with pytest.raises(module.ArchiveError, match='tracked'):
        module.archive(exp, destination=dest, prune=True, yes=True)


def test_empty_directories_and_broken_symlinks(experiment):
    _, exp, dest = experiment
    (exp/'empty').mkdir()
    (exp/'broken-link').symlink_to('missing-target')
    final = module.archive(exp, destination=dest)
    with zipfile.ZipFile(final) as bundle:
        assert bundle.getinfo('experiments/finished/empty/').is_dir()
        assert bundle.read('experiments/finished/broken-link') == b'missing-target'


def test_zip_count_failure_blocks_prune(experiment, monkeypatch):
    _, exp, dest = experiment
    original = zipfile.ZipFile.infolist
    def wrong_count(self):
        result = original(self)
        return result[:-1] if self.mode == 'r' else result
    monkeypatch.setattr(zipfile.ZipFile, 'infolist', wrong_count)
    with pytest.raises(module.ArchiveError, match='count or uncompressed size'):
        module.archive(exp, destination=dest, prune=True, yes=True)
    assert (exp/'results/evidence.json').exists()


def test_index_failure_blocks_prune(experiment):
    _, exp, dest = experiment
    (dest/'archive-index.csv').write_text('unexpected,columns\n')
    with pytest.raises(module.ArchiveError, match='index header'):
        module.archive(exp, destination=dest, prune=True, yes=True)
    assert (exp/'results/evidence.json').exists()
    assert not (exp/'ARCHIVED.json').exists()


def test_destination_created_during_publish_is_never_overwritten(experiment, monkeypatch):
    _, exp, dest = experiment
    original = module.rename_exclusive
    collisions = []
    def race(source, target):
        target.write_text('another writer')
        collisions.append(target)
        original(source, target)
    monkeypatch.setattr(module, 'rename_exclusive', race)
    with pytest.raises(module.ArchiveError, match='File exists'):
        module.archive(exp, destination=dest, prune=True, yes=True)
    assert collisions[0].read_text() == 'another writer'
    assert (exp/'results/evidence.json').exists()
    assert not (dest/'archive-index.csv').exists()


def test_run_lock_is_held_through_destination_verification(experiment, monkeypatch):
    _, exp, dest = experiment
    original = module.copy_verified
    def copy(*args):
        with (exp/'state/run.lock').open('rb') as stream:
            with pytest.raises(BlockingIOError):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return original(*args)
    monkeypatch.setattr(module, 'copy_verified', copy)
    module.archive(exp, destination=dest)


def test_prune_preserves_git_precomposed_unicode_names(experiment):
    repo, exp, dest = experiment
    tracked = put(exp, 'docs/cafe\u0301.md', 'tracked Unicode report')
    git(repo, '-c', 'core.precomposeunicode=true', 'add', str(exp/'docs'))
    module.archive(exp, destination=dest, prune=True, yes=True)
    assert tracked.read_text() == 'tracked Unicode report'
    assert (exp/'docs/report.md').read_text() == 'tracked report'


def owned_pool(exp):
    pool = exp/'results/policy/pools/group/1'
    write_json(pool/'state/run-owner.json', dict(experiment=str(exp), run_id='previous-run'))
    put(pool, 'generated/output.txt', 'paid output')
    return pool


@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('active', ['lock', 'running', 'stopping'])
def test_owned_pool_refuses_active_coordinator(experiment, dry_run, active):
    _, exp, dest = experiment
    pool = owned_pool(exp)
    # A new run ID still owns the same workspace; old pool records cannot bypass it.
    write_json(exp/'state/run.json', dict(status='running' if active == 'running' else 'completed',
                                        run_id='new-run'))
    if active == 'stopping':
        write_json(exp/'state/stopping.json', {})
    before = {p: p.read_bytes() for p in exp.rglob('*') if p.is_file()}
    with (exp/'state/run.lock').open('rb') as owner:
        if active == 'lock':
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(module.ArchiveError, match='running|locked|cleanup'):
            module.archive(pool, destination=dest, prune=True, yes=True, dry_run=dry_run)
    assert before == {p: p.read_bytes() for p in exp.rglob('*') if p.is_file()}
    assert not list(dest.iterdir())


def test_completed_owner_is_locked_through_pool_pruning(experiment, monkeypatch):
    _, exp, dest = experiment
    pool = owned_pool(exp)
    owner_files = {p: p.read_bytes() for p in (exp/'state').iterdir()}
    original = module.prune_files
    def prune(*args, **kwargs):
        result = original(*args, **kwargs)
        # This runs after deletion, before ARCHIVED.json and lock release.
        for path in (exp/'state/run.lock', pool/'state/run.lock'):
            with path.open('rb') as stream:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return result
    monkeypatch.setattr(module, 'prune_files', prune)
    module.archive(pool, destination=dest, prune=True, yes=True)
    assert not (pool/'generated/output.txt').exists()
    assert (pool/'state/run.lock').is_file()
    assert owner_files == {p: p.read_bytes() for p in (exp/'state').iterdir()}


@pytest.mark.parametrize('owner_kind', ['missing-lock', 'self', 'unrelated', 'malformed'])
def test_pool_with_unguardable_owner_is_rejected(experiment, owner_kind):
    _, exp, dest = experiment
    pool = owned_pool(exp)
    if owner_kind == 'missing-lock':
        (exp/'state/run.lock').unlink()
    else:
        value = {'self': str(pool), 'unrelated': str(dest), 'malformed': None}[owner_kind]
        write_json(pool/'state/run-owner.json', dict(experiment=value))
    with pytest.raises(module.ArchiveError, match='owner|owning'):
        module.archive(pool, destination=dest, prune=True, yes=True)
    assert (pool/'generated/output.txt').read_text() == 'paid output'
    assert not list(dest.iterdir())


@pytest.mark.parametrize('include_work', [False, True])
@pytest.mark.parametrize('group', ['target', 'dist', 'checkouts', 'compile-cache', 'work', 'normal'])
def test_pool_evidence_is_archived_before_pruning(experiment, group, include_work):
    repo, exp, dest = experiment
    pool = exp/f'results/policy/pools/{group}/1'
    paid = [put(pool, f'generated/{name}/output.txt', 'paid '+name)
            for name in ('target', 'checkouts', 'compile-cache', 'work')]
    metadata = put(pool, 'funnel.json', '{}')
    scratch = put(pool, 'work/scratch.txt', 'scratch')
    cache = put(pool, 'target/cache.txt', 'regenerable')
    nested_cache = put(pool, 'work/compile-cache/cache.txt', 'regenerable')
    final = module.archive(exp, destination=dest, include_work=include_work, prune=True, yes=True)
    with zipfile.ZipFile(final) as bundle:
        for path in [*paid, metadata]:
            assert path.relative_to(repo).as_posix() in bundle.namelist()
            assert not path.exists()
        for path in paid:
            assert bundle.read(path.relative_to(repo).as_posix()).startswith(b'paid ')
        assert (scratch.relative_to(repo).as_posix() in bundle.namelist()) == include_work
        assert cache.relative_to(repo).as_posix() not in bundle.namelist()
        assert nested_cache.relative_to(repo).as_posix() not in bundle.namelist()


def test_pruning_keeps_all_acquired_lock_inodes(experiment, monkeypatch):
    _, exp, dest = experiment
    names = ['state/run.lock', 'state/stop.lock', 'state/policy-run.lock',
             'state/policy/run.lock', 'results/policy/state/run.lock']
    paths = [put(exp, name, '') for name in names]
    inodes = {path: path.stat().st_ino for path in paths}
    put(exp, 'work/to-delete.txt')
    with ExitStack() as stack:
        # Existing descriptors must refer to the same permanent lock after pruning.
        clients = [stack.enter_context(path.open('rb')) for path in paths]
        original = module.remove_retry
        def remove(path, **kwargs):
            original(path, **kwargs)
            for lock, client in zip(paths, clients):
                assert lock.stat().st_ino == inodes[lock]
                with pytest.raises(BlockingIOError):
                    fcntl.flock(client, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(module, 'remove_retry', remove)
        module.archive(exp, destination=dest, prune=True, yes=True)
        for lock, client in zip(paths, clients):
            assert lock.stat().st_ino == inodes[lock]
            fcntl.flock(client, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not (exp/'work').exists()


def valid_config(exp):
    put(exp, 'experiment.yaml', f'name: archive-test\nanchor: t\nproject:\n  repo: {exp}\n')


@pytest.mark.parametrize('watch', [False, True])
def test_restored_archive_shows_stage_status(experiment, capsys, watch):
    repo, exp, dest = experiment
    valid_config(exp)
    final = module.archive(exp, destination=dest, prune=True, yes=True)
    capsys.readouterr()
    with zipfile.ZipFile(final) as bundle:
        bundle.extractall(repo)
    assert (exp/'ARCHIVED.json').exists()
    assert main(['status', str(exp), *(['--watch', '--no-panel'] if watch else [])]) == 0
    output = capsys.readouterr().out
    assert 'archived to' not in output
    assert 'report     stale' in output  # The restored fixture has no config hash.


@pytest.mark.parametrize('watch', [False, True])
def test_new_run_overrides_marker_with_tracked_configuration(experiment, capsys, monkeypatch, watch):
    from test_survival.pipeline import run_session
    repo, exp, dest = experiment
    valid_config(exp)
    git(repo, 'add', str(exp/'experiment.yaml'), str(exp/'state/run.json'))
    final = module.archive(exp, destination=dest, prune=True, yes=True)
    capsys.readouterr()
    assert main(['status', str(exp)]) == 0
    assert capsys.readouterr().out.strip() == f'archived to {final}'
    with run_session(exp):
        if watch:
            # Render one real watch iteration, then finish the synthetic run.
            def finish(_):
                write_json(exp/'state/run.json', dict(status='completed'))
            monkeypatch.setattr('test_survival.panel.time.sleep', finish)
        assert main(['status', str(exp), *(['--watch', '--no-panel'] if watch else [])]) == 0
        output = capsys.readouterr().out
        assert 'archived to' not in output
        assert 'Process' in output and 'alive' in output
    assert main(['status', str(exp)]) == 0
    assert 'archived to' not in capsys.readouterr().out


def test_init_after_pruning_overrides_marker(experiment, capsys):
    _, exp, dest = experiment
    module.archive(exp, destination=dest, prune=True, yes=True)
    assert main(['init', str(exp)]) == 0
    capsys.readouterr()
    assert main(['status', str(exp)]) == 0
    output = capsys.readouterr().out
    assert 'archived to' not in output and 'pending' in output


def test_prune_rejects_symlinked_lock_parent(experiment):
    _, exp, dest = experiment
    target = exp/'lock-target'
    put(target, 'run.lock', '')
    (exp/'state/policy').symlink_to(target, target_is_directory=True)
    with pytest.raises(module.ArchiveError, match='symlinked run lock'):
        module.archive(exp, destination=dest, prune=True, yes=True)
    assert (target/'run.lock').is_file()
    assert (exp/'results/evidence.json').exists()


def test_legacy_marker_still_reports_archive_without_state(experiment, capsys):
    _, exp, dest = experiment
    final = module.archive(exp, destination=dest, prune=True, yes=True)
    path = exp/'ARCHIVED.json'
    marker = json.loads(path.read_text())
    marker.pop('retained_status_files')
    write_json(path, marker)
    capsys.readouterr()
    assert main(['status', str(exp)]) == 0
    assert capsys.readouterr().out.strip() == f'archived to {final}'


@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('record', ['null', 'false', '[]', '""'])
def test_present_nonobject_owner_cannot_bypass_coordinator(experiment, record, dry_run):
    _, exp, dest = experiment
    pool = owned_pool(exp)
    put(pool, 'state/run-owner.json', record)
    write_json(exp/'state/run.json', dict(status='running', run_id='active'))
    before = {p: p.read_bytes() for p in exp.rglob('*') if p.is_file()}
    with (exp/'state/run.lock').open('rb') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(module.ArchiveError, match='Invalid state/run-owner.json'):
            module.archive(pool, destination=dest, prune=True, yes=True, dry_run=dry_run)
    assert before == {p: p.read_bytes() for p in exp.rglob('*') if p.is_file()}
    assert not list(dest.iterdir())


@pytest.mark.parametrize('broken', [False, True])
def test_symlinked_owner_is_invalid_not_missing(experiment, tmp_path, broken):
    _, exp, dest = experiment
    pool = owned_pool(exp)
    external = tmp_path/'owner.json'
    if not broken:
        external.write_text('null')
    (pool/'state/run-owner.json').unlink()
    (pool/'state/run-owner.json').symlink_to(external)
    with pytest.raises(module.ArchiveError, match='Invalid symlinked run-owner.json'):
        module.archive(pool, destination=dest, prune=True, yes=True)
    assert (pool/'generated/output.txt').exists()
    assert not list(dest.iterdir())


@pytest.mark.parametrize('prune', [False, True])
def test_state_symlinks_do_not_embed_external_json(experiment, tmp_path, prune):
    repo, exp, dest = experiment
    secret = 'EXTERNAL_PRIVATE_CONTENT_MUST_NOT_BE_EMBEDDED'
    external = put(tmp_path, 'outside/private.json', json.dumps({'secret': secret}))
    (exp/'state/extra.json').symlink_to(external)
    (exp/'state/broken.json').symlink_to(tmp_path/'missing.json')
    final = module.archive(exp, destination=dest, prune=prune, yes=True)
    with zipfile.ZipFile(final) as bundle:
        manifest_bytes = bundle.read('ARCHIVE-MANIFEST.json')
        manifest = json.loads(manifest_bytes)
        assert 'extra.json' not in manifest['stage_states']
        assert 'broken.json' not in manifest['stage_states']
        assert manifest['stage_states']['report.json']['status'] == 'completed'
        assert secret.encode() not in manifest_bytes
        name = (exp/'state/extra.json').relative_to(repo).as_posix()
        assert stat.S_ISLNK(bundle.getinfo(name).external_attr >> 16)
        assert bundle.read(name) == os.fsencode(external)
    assert secret not in final.with_suffix('.manifest.json').read_text()
    assert json.loads(external.read_text())['secret'] == secret


def test_metadata_open_does_not_follow_file_swapped_to_symlink(experiment, tmp_path, monkeypatch):
    _, exp, dest = experiment
    extra = put(exp, 'state/extra.json', '{"status": "completed"}')
    external = put(tmp_path, 'outside/private.json', '{"secret":"PRIVATE_SWAP_CONTENT"}')
    original = os.open
    swapped = False
    def swap(name, flags, *args, **kwargs):
        nonlocal swapped
        if name == 'extra.json' and kwargs.get('dir_fd') is not None and not swapped:
            extra.unlink()
            extra.symlink_to(external)
            swapped = True
        return original(name, flags, *args, **kwargs)
    monkeypatch.setattr(module.os, 'open', swap)
    final = module.archive(exp, destination=dest)
    assert swapped
    with zipfile.ZipFile(final) as bundle:
        data = bundle.read('ARCHIVE-MANIFEST.json')
        assert b'PRIVATE_SWAP_CONTENT' not in data
        assert 'extra.json' not in json.loads(data)['stage_states']


def test_metadata_open_rejects_swapped_state_directory(experiment, tmp_path, monkeypatch):
    _, exp, dest = experiment
    external = tmp_path/'outside'
    put(external, 'private.json', '{"secret":"PRIVATE_DIRECTORY_CONTENT"}')
    original = os.open
    swapped = False
    def swap(name, flags, *args, **kwargs):
        nonlocal swapped
        if name == 'state' and kwargs.get('dir_fd') is not None and not swapped:
            (exp/'state').rename(exp/'saved-state')
            (exp/'state').symlink_to(external, target_is_directory=True)
            swapped = True
        return original(name, flags, *args, **kwargs)
    monkeypatch.setattr(module.os, 'open', swap)
    with pytest.raises(module.ArchiveError, match='Archive failed'):
        module.archive(exp, destination=dest, prune=True, yes=True)
    assert swapped
    assert (external/'private.json').read_text() == '{"secret":"PRIVATE_DIRECTORY_CONTENT"}'
    assert not list(dest.iterdir())


def test_archived_status_never_hashes_tracked_symlink_targets(experiment, tmp_path, capsys):
    repo, exp, dest = experiment
    external = put(tmp_path, 'outside/private.json', '{"secret":"original"}')
    link = exp/'state/extra.json'
    link.symlink_to(external)
    git(repo, 'add', str(link))
    final = module.archive(exp, destination=dest, prune=True, yes=True)
    marker = json.loads((exp/'ARCHIVED.json').read_text())
    assert 'state/extra.json' not in marker['retained_status_files']
    external.write_bytes(b'not even JSON\xff')
    capsys.readouterr()
    assert main(['status', str(exp)]) == 0
    assert capsys.readouterr().out.strip() == f'archived to {final}'


@pytest.mark.parametrize('location', ['work/victim', 'results/victim'])
@pytest.mark.parametrize('timing', ['before-open', 'after-open'])
def test_pruning_directory_swap_cannot_touch_external_tree(experiment, tmp_path, monkeypatch, location, timing):
    _, exp, dest = experiment
    victim = exp/location
    put(victim, 'local.txt', 'local scratch')
    external = tmp_path/'outside'
    secret = put(external, 'private.txt', 'outside data')
    secret.chmod(0o444)
    before = {p: (p.read_bytes(), p.stat().st_mode) for p in external.iterdir()}
    original_prune, original_open = module.prune_files, os.open
    swapped = False
    def prune(*args, **kwargs):
        def swap(name, flags, *open_args, **open_kwargs):
            nonlocal swapped
            if name == 'victim' and flags & os.O_DIRECTORY and not swapped:
                fd = original_open(name, flags, *open_args, **open_kwargs) if timing == 'after-open' else None
                victim.rename(victim.with_name('original'))
                victim.symlink_to(external, target_is_directory=True)
                swapped = True
                if fd is not None:
                    return fd
            return original_open(name, flags, *open_args, **open_kwargs)
        with monkeypatch.context() as context:
            context.setattr(module.os, 'open', swap)
            return original_prune(*args, **kwargs)
    monkeypatch.setattr(module, 'prune_files', prune)
    with pytest.raises(module.ArchiveError):
        module.archive(exp, destination=dest, prune=True, yes=True)
    assert swapped
    assert before == {p: (p.read_bytes(), p.stat().st_mode) for p in external.iterdir()}
    assert not (exp/'ARCHIVED.json').exists()
    assert list(dest.rglob('*.zip'))  # A verified archive exists even if pruning aborts.


def test_prune_and_marker_stay_anchored_when_root_path_is_swapped(experiment, tmp_path, monkeypatch):
    repo, exp, dest = experiment
    tracked = put(exp, 'docs/cafe\u0301.md', 'tracked Unicode report')
    git(repo, '-c', 'core.precomposeunicode=true', 'add', str(exp/'docs'))
    external = tmp_path/'outside'
    put(external, 'private.txt', 'outside data')
    put(external, 'ARCHIVED.json', 'outside marker')
    original = module.prune_files
    moved = exp.with_name('moved-experiment')
    def swap(*args, **kwargs):
        exp.rename(moved)
        exp.symlink_to(external, target_is_directory=True)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'prune_files', swap)
    module.archive(exp, destination=dest, prune=True, yes=True)
    assert (external/'private.txt').read_text() == 'outside data'
    assert (external/'ARCHIVED.json').read_text() == 'outside marker'
    assert (moved/'ARCHIVED.json').is_file()
    assert (moved/'docs/report.md').read_text() == 'tracked report'
    assert (moved/tracked.relative_to(exp)).read_text() == 'tracked Unicode report'


@pytest.mark.parametrize('existing_row', [False, True])
@pytest.mark.parametrize('ending', ['', '\n', '\r\n', '\r'])
def test_index_append_preserves_unterminated_csv(experiment, existing_row, ending):
    import io
    _, exp, dest = experiment
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=module.INDEX_FIELDS, lineterminator='\n')
    writer.writeheader()
    old_row = dict.fromkeys(module.INDEX_FIELDS, 'old')
    old_row.update(fork='caf\u00e9', exp_dir='a path, with\na newline')
    if existing_row:
        writer.writerow(old_row)
    (dest/'archive-index.csv').write_bytes((buffer.getvalue().rstrip('\n')+ending).encode())
    final = module.archive(exp, destination=dest, prune=True, yes=True)
    with (dest/'archive-index.csv').open(newline='') as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        assert reader.fieldnames == list(module.INDEX_FIELDS)
    assert len(rows) == 1+existing_row
    if existing_row:
        assert rows[0] == old_row
    assert set(rows[-1]) == set(module.INDEX_FIELDS)
    assert rows[-1]['sha256'] == module.sha256(final)
    assert dest/rows[-1]['zip_relative_path'] == final
    assert not (exp/'generated/model.txt').exists()


def test_state_directory_with_json_suffix_is_archived_without_metadata_read(experiment):
    repo, exp, dest = experiment
    evidence = put(exp, 'state/attachments.json/evidence.txt', 'regular evidence')
    final = module.archive(exp, destination=dest)
    with zipfile.ZipFile(final) as bundle:
        manifest = json.loads(bundle.read('ARCHIVE-MANIFEST.json'))
        assert 'attachments.json' not in manifest['stage_states']
        assert bundle.read(evidence.relative_to(repo).as_posix()) == b'regular evidence'


def test_nonregular_owner_is_rejected_without_blocking(experiment):
    _, exp, dest = experiment
    pool = owned_pool(exp)
    path = pool/'state/run-owner.json'
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(module.ArchiveError, match='expected a regular file'):
        module.archive(pool, destination=dest, prune=True, yes=True)
    assert (pool/'generated/output.txt').exists()


def test_prune_reuses_identical_existing_archive(experiment, capsys):
    _, exp, dest = experiment
    first = module.archive(exp, destination=dest)
    capsys.readouterr()
    final = module.archive(exp, destination=dest, prune=True, yes=True)
    assert final == first
    assert 'Reusing existing archive' in capsys.readouterr().out
    assert sorted(p.name for p in first.parent.glob('*.zip')) == [first.name]
    with open(dest/'archive-index.csv', newline='') as stream:
        assert len(list(csv.DictReader(stream))) == 1
    assert not (exp/'results/evidence.json').exists()
    assert json.loads((exp/'ARCHIVED.json').read_text())['destination'] == str(first)


def test_prune_writes_new_archive_after_change(experiment):
    _, exp, dest = experiment
    first = module.archive(exp, destination=dest)
    put(exp, 'results/evidence.json', '{"answer": 43}')
    final = module.archive(exp, destination=dest, prune=True, yes=True)
    assert final != first
    with zipfile.ZipFile(final) as bundle:
        assert bundle.read('experiments/finished/results/evidence.json') == b'{"answer": 43}'


def test_prune_does_not_reuse_damaged_or_forced(experiment, capsys):
    _, exp, dest = experiment
    first = module.archive(exp, destination=dest)
    with first.open('ab') as stream:
        stream.write(b'x')
    second = module.archive(exp, destination=dest)
    third = module.archive(exp, destination=dest, prune=True, yes=True, new_zip=True)
    assert len({first, second, third}) == 3
    assert 'Reusing' not in capsys.readouterr().out


def test_damaged_archive_falls_back_to_new_zip(experiment, capsys):
    _, exp, dest = experiment
    first = module.archive(exp, destination=dest)
    with first.open('ab') as stream:
        stream.write(b'x')
    capsys.readouterr()
    final = module.archive(exp, destination=dest, prune=True, yes=True)
    assert final != first
    assert 'Reusing' not in capsys.readouterr().out


def test_reuse_rejects_index_path_inside_experiment(experiment):
    _, exp, dest = experiment
    first = module.archive(exp, destination=dest)
    moved = exp/'work'/first.name
    moved.parent.mkdir(parents=True, exist_ok=True)
    first.rename(moved)
    first.with_suffix('.manifest.json').rename(moved.with_suffix('.manifest.json'))
    index = dest/'archive-index.csv'
    rows = list(csv.DictReader(index.open(newline='')))
    for path in (str(moved), os.path.relpath(moved, dest)):
        rows[-1]['zip_relative_path'] = path
        with index.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=module.INDEX_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        e, d = exp.resolve(), dest.resolve()
        rules = module.manifest_for(e, e.parents[1], False)['exclusion_rules']
        assert module.reusable_archive(d, e.parents[1], e, False, rules) is None
    final = module.archive(exp, destination=dest, prune=True, yes=True)
    assert final.is_relative_to(dest) and final.exists()


def test_reuse_distinguishes_symlink_from_regular_file(experiment):
    _, exp, dest = experiment
    put(exp, 'results/other.txt', 'x')
    put(exp, 'results/entry', 'other.txt')
    first = module.archive(exp, destination=dest)
    (exp/'results/entry').unlink()
    (exp/'results/entry').symlink_to('other.txt')
    final = module.archive(exp, destination=dest, prune=True, yes=True)
    assert final != first
    with zipfile.ZipFile(final) as bundle:
        assert stat.S_ISLNK(bundle.getinfo('experiments/finished/results/entry').external_attr >> 16)


@pytest.mark.parametrize('content', ['null', '[]', '"text"', '\xff'])
def test_reuse_skips_non_object_sidecar(experiment, content):
    _, exp, dest = experiment
    first = module.archive(exp, destination=dest)
    sidecar = first.with_suffix('.manifest.json')
    sidecar.write_bytes(content.encode('latin-1'))
    final = module.archive(exp, destination=dest, prune=True, yes=True)
    assert final != first and final.exists()
