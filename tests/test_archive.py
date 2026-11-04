import csv
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import zipfile

import pytest

from test_survival import archive as module
from test_survival.cli import main
from test_survival.display import load_display_settings, settings_path


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
    assert remaining == {'docs/report.md', 'work/tracked.txt', 'ARCHIVED.json'}
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
