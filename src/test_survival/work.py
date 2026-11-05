"""Success-only workspace release. Callers own the experiment run lock.

A completed result is authoritative; work is expendable only after its consumers
validate it. Published locks, model output and unknown paths are never removed.
"""
import os
from pathlib import Path
import shutil
import stat

from .io import read_json


def _remove_owned(exp, relative):
    """Delete beneath an open experiment descriptor without following parent links."""
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts or relative.parts[:1] != ('work',):
        raise ValueError('Cleanup requires an experiment-relative work path')
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError('Safe workspace deletion is unavailable on this platform')
    fd = os.open(exp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return
            os.close(fd); fd = child
        name = relative.name
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError('Refusing symlink workspace: '+str(relative))
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


def remove_owned(exp, relative, config=None):
    """Delete owned work, including Linux files created by the root container."""
    try:
        _remove_owned(exp, relative)
    except PermissionError:
        # Host coordinators cannot traverse/unlink root-owned Maven/worker
        # subtrees on Linux. Re-run the same descriptor-relative, no-follow
        # deletion in the configured container; never change tree permissions.
        if os.geteuid() == 0:
            raise
        from uuid import uuid4
        from .config import load
        from .runner.docker import DockerRunner
        from .io import write_json
        exp=Path(exp)
        # Nested policy pools have no YAML; their caller supplies the same
        # effective runner settings (including generator overrides) as the build.
        if config is None:
            if not (exp/'experiment.yaml').is_file():
                raise
            config=load(exp)
        runner=DockerRunner(exp,config)
        script=('import importlib,sys; sys.path.insert(0,"/"); '
                'importlib.import_module("survival-package.work").remove_owned(sys.argv[1],sys.argv[2])')
        log=exp/'results/work-cleanup-commands'/(uuid4().hex+'.log')
        result=runner.execute(['python3','-c',script,str(exp),str(relative)],exp,log,600)
        write_json(log.with_suffix('.json'),result)
        if result['exit_code']:
            raise RuntimeError('Container workspace cleanup failed; see '+str(log))


def release_checkout(exp, config, index, *, final=False):
    """Release a verified index after all consumers and coordinator checks succeed."""
    exp = Path(exp)
    if getattr(config, 'keep_work', False) or (int(index) == 0 and not final):
        return
    record = read_json(exp/f'results/builds/{index}.json', {})
    identity = read_json(exp/f'results/workspace-identities/{index}.json', {})
    # Old experiments lacking a trustworthy dependency identity are readable but
    # are not retroactively pruned. Never lose a failed build's working evidence.
    if record.get('build') != 'ok' or not identity or identity.get('sha') != record.get('sha'):
        return
    remove_owned(exp, f'work/checkouts/{index}', config)
    remove_owned(exp, f'work/checkouts/{index}.json', config)


def finish_checkouts(exp, config):
    if getattr(config, 'keep_work', False):
        return
    root = Path(exp)/'work/checkouts'
    if root.is_symlink():
        raise RuntimeError('Refusing symlink checkouts')
    if not root.exists():
        return
    for path in sorted(root.glob('*.json')):
        if path.stem.isdigit():
            release_checkout(exp, config, path.stem, final=True)
    if root.exists() and not any(root.iterdir()):
        remove_owned(exp, 'work/checkouts', config)


def footprint(path):
    files = size = 0
    if path.is_symlink():
        raise RuntimeError('Refusing symlink work evidence')
    if path.is_file():
        return dict(files=1, bytes=path.stat().st_size)
    for folder, directories, names in os.walk(path, followlinks=False):
        for name in [*directories, *names]:
            p = Path(folder)/name
            if p.is_symlink():
                raise RuntimeError('Refusing symlink work evidence')
        for name in names:
            files += 1; size += (Path(folder)/name).stat().st_size
    return dict(files=files, bytes=size)


def failed(value):
    if isinstance(value, dict):
        if value.get('exit_code', 0) != 0 or value.get('timeout'):
            return True
        if value.get('state') in ('fail', 'error', 'timeout', 'absent', 'compile-fail'):
            return True
        if value.get('status') in ('failed', 'error', 'coverage-fail') or value.get('coverage_status') == 'missing':
            return True
        return any(failed(v) for v in value.values())
    return isinstance(value, list) and any(failed(v) for v in value)


def record_cleanup(exp, removed, retained):
    from .io import write_json
    path = Path(exp)/'results/work-cleanup.json'
    prior = read_json(path, dict(removed_files=0, removed_bytes=0, retained_failures={}))
    prior['removed_files'] += removed['files']; prior['removed_bytes'] += removed['bytes']
    prior['retained_failures'].update(retained)
    # A retained parent supersedes children to avoid counting their bytes twice.
    paths = prior['retained_failures']
    prior['retained_failures'] = {k:v for k,v in paths.items()
                                  if not any(k.startswith(other+'/') for other in paths if other != k)}
    write_json(path, prior)


def cleanup_job(exp, job_path, config=None):
    """Keep failed file subtrees whole. Unknown/missing results never permit pruning."""
    exp = Path(exp)
    job = read_json(job_path, {})
    if not job.get('output') or not job.get('work'):
        return
    relative = Path(job['work'])
    if relative.parts[:1] != ('work',) or '..' in relative.parts or relative.is_absolute():
        raise ValueError('Unsafe worker work path')
    root = exp/relative
    if not root.exists():return
    result = read_json(exp/job['output'])
    if result is None:return
    protected = set()
    if failed(result):
        # Baseline worker files have private numbered directories. Preserve each
        # failed file including its classes, reports and logs; other files may go.
        if result.get('files') and job.get('action') == 'run':
            for i, record in enumerate(result['files']):
                if failed(record):protected.add(str(i))
            suite = result.get('whole_frozen_suite_compile')
            # Successful whole-suite compilation supplies the actual classes
            # to every numbered file; its private classes directory is empty.
            if suite is not None and (protected or failed(suite)):
                protected.update(p.name for p in root.glob('suite-*'))
            if failed(result.get('coverage_report')):protected.add('.')
            if not protected:protected.add('.')
        else:
            # Policy batches share compiled snapshots/chunks; keep the whole
            # failed batch rather than guessing which shared evidence is safe.
            protected.add('.')
    removed = dict(files=0,bytes=0); retained = {}
    if '.' in protected:
        retained[str(relative)] = footprint(root)
    else:
        for child in list(root.iterdir()):
            if child.name in protected:
                retained[str(child.relative_to(exp))] = footprint(child)
                continue
            counts = footprint(child)
            remove_owned(exp, child.relative_to(exp), config)
            for key in removed:removed[key] += counts[key]
        if not any(root.iterdir()):remove_owned(exp,relative,config)
    if not retained:
        for path in (Path(job_path), Path(job_path).with_suffix('.log')):
            if path.exists():
                counts = footprint(path);remove_owned(exp,path.relative_to(exp),config)
                for key in removed:removed[key] += counts[key]
    record_cleanup(exp,removed,retained)


def cleanup_jobs(exp, config, bases):
    if getattr(config, 'keep_work', False):return
    exp = Path(exp)
    for base in bases:
        root = exp/'work'/base
        if root.is_symlink():raise RuntimeError('Refusing symlink scratch')
        if root.is_dir():
            for job in sorted(root.rglob('*-job.json')):
                cleanup_job(exp,job,config)
        job = root.with_name(root.name+'-job.json')
        if job.is_file():cleanup_job(exp,job,config)


def cleanup_stage(exp, config, stage):
    bases = {'scope':['dev-method-coverage-t'], 'generate':['validation','import-repair'],
             'screen':['validation'], 'control':['phase-control'], 'survive':['phase-A'],
             'cover':['phase-B'], 'policy-survive':['policy-survive']}.get(stage, [])
    cleanup_jobs(exp,config,bases)


def retain_previous_work(exp, relative):
    """A resumed worker must not erase its interrupted attempt's scratch."""
    from uuid import uuid4
    from .io import write_json
    exp = Path(exp); relative = Path(relative)
    if relative.parts[:1] != ('work',) or relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Unsafe worker work path')
    root = exp/relative
    counts = footprint(root) if root.exists() else dict(files=0, bytes=0)
    siblings = [root.with_name(root.name+suffix) for suffix in ('-job.json', '-job.log')]
    if not counts['files'] and not any(p.is_file() for p in siblings):return
    target = exp/'work/failed-attempts'/str(relative.relative_to('work'))/uuid4().hex
    target.parent.mkdir(parents=True, exist_ok=True)
    # Parent chain is validated before copying; run ownership excludes other writers.
    if any(p.is_symlink() for p in [root, *root.parents] if p != exp.parent and p.is_relative_to(exp)):
        raise RuntimeError('Refusing symlink scratch')
    # Docker Desktop may retain a guest dentry after a host rename. Moving
    # would let the next worker's rmtree reach the moved evidence (or fail on
    # its stale name). Copy to independent inodes; the worker owns clearing
    # the unchanged original directory after this durable snapshot is complete.
    if root.exists():shutil.copytree(root, target)
    else:target.mkdir()
    for suffix in ('-job.json', '-job.log'):
        sibling = root.with_name(root.name+suffix)
        if sibling.is_file():
            if sibling.is_symlink():raise RuntimeError('Refusing symlink job evidence')
            shutil.copy2(sibling, target.with_name(target.name+suffix))
    write_json(target.with_suffix('.json'),dict(original=str(relative),reason='retained before retry',**counts))


def finish_work(exp, config, *, pool=False):
    """Only invoked after whole-run or verified-pool success; whitelist owned roots."""
    if getattr(config, 'keep_work', False):return
    exp=Path(exp)
    cleanup_jobs(exp,config,['phase-A','phase-B','phase-control','validation','import-repair',
                             'dev-method-coverage-t','dev-screen','policy-coverage','policy-survive'])
    removed=dict(files=0,bytes=0)
    retained=read_json(exp/'results/work-cleanup.json',{}).get('retained_failures',{})
    for name in ('libs','classpaths','classpath-jobs','runner-setup-job.json','runner-setup-job.log',
                 *(() if retained else ('runtime',))):
        path=exp/'work'/name
        if path.exists():
            counts=footprint(path);remove_owned(exp,path.relative_to(exp),config)
            for key in removed:removed[key]+=counts[key]
    # Keep KTester's report sentinel and completion record in place. Preparation
    # and round scratch can go once archived; generation reuses completed archives
    # before consulting its disposable worker markers or accessing credentials.
    for dataset in (exp/'work/ktester').glob('*'):
        if not dataset.is_dir():continue
        if not pool and not (dataset/'experiment-result.json').is_file():continue
        rounds=dataset/'rounds'
        verified=True
        for folder in rounds.glob('*'):
            if not folder.is_dir():continue
            if read_json(folder/'run.json',{}).get('status')!='completed':verified=False;break
            for file in folder.rglob('*'):
                if file.is_file() and file.suffix not in ('.class','.jar'):
                    archived=exp/'generated'/dataset.name/folder.name/file.relative_to(folder)
                    if not archived.is_file() or archived.read_bytes()!=file.read_bytes():verified=False;break
        if not verified:continue
        for child in list(dataset.iterdir()):
            if child.name in ('experiment-result.json','dataset_info.json'):continue
            counts=footprint(child);remove_owned(exp,child.relative_to(exp),config)
            for key in removed:removed[key]+=counts[key]
    failures = {}
    for name in ('failed-attempts', *(() if not retained else ('runtime',))):
        path = exp/'work'/name
        if path.exists():failures[str(path.relative_to(exp))] = footprint(path)
    record_cleanup(exp,removed,failures)
    # compile-cache, generation-history, policy-history, failed-attempts and
    # unknown work are intentionally untouched, including nested generated data.
