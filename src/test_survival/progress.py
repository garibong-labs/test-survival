"""Read-only stage progress from existing state and evidence records."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .io import read_csv, read_json


def csv_rows(path):
    return read_csv(path) if path.exists() else []


def items(exp, stage, config, state):
    """Return completed items as lists of evidence paths, total, and unit.

    A commit/index is done only when all measured groups have records. Empty
    lists represent build failures that require no work in this stage.
    """
    completed = []
    if stage == 'prepare':
        commits = csv_rows(exp/'results/commits.csv')
        total = len(commits)+1 if (exp/'results/commits.csv').exists() else None
        for index in ['0', *(r['index'] for r in commits)]:
            path = exp/f'results/builds/{index}.json'
            if read_json(path, {}).get('build') in ('ok', 'fail', 'skipped-untouched'):
                completed.append([path])
        return completed, total, 'builds'
    if stage in ('generate', 'screen'):
        # Import locally: importer uses pipeline logging.
        from .importer import rounds
        total = 0
        unknown = False
        scope = read_json(exp/'population/scope.json', {})
        for group in config['generators']:
            if stage == 'generate' and state.get('group') not in (None, group['name']):
                continue
            root = exp/'generated'/group['name']
            directories = rounds(root)
            if stage == 'screen':
                for directory in directories:
                    trees = sorted(directory.glob('*/test_classes')) if group['method'] == 'ktester' else []
                    sources = sorted(trees[0].glob('*.java')) if trees else sorted((directory/'sources').rglob('*.java'))
                    total += len(sources)
                    for number, source in enumerate(sources):
                        path = directory/f'validation/{number}.json'
                        if read_json(path, {}).get('file') == str(source.relative_to(exp)):
                            completed.append([path])
                continue
            manifest = read_json(root/'rounds.json', {})
            paths = {}
            if group['method'] == 'ouedraogo':
                for directory in directories:
                    for path in directory.glob('runs/*/*/**/run.json'):
                        if read_json(path, {}).get('status'):
                            paths[str(path.relative_to(root))] = path
                retries = sum('/attempt-2/' in key for key in paths)
                planned = len(scope.get('classes', []))*len(group['params']['prompts'])*group['rounds']['max'] + retries
                if not scope and not manifest.get('stopped'):
                    unknown = True
            else:
                # KTester writes live calls under work/, then archives them.
                # Deduplicate the live/archive copies by round and call number.
                for base in (exp/'work/ktester'/group['name']/'rounds', root):
                    for directory in rounds(base):
                        for path in directory.glob('raw-responses/*/call.json'):
                            if read_json(path, {}).get('status'):
                                paths[str(path.relative_to(base))] = path
                planned = len(paths)
                # Repair/retry calls are adaptive; the total is not known yet.
                if not manifest.get('stopped'):
                    unknown = True
            completed.extend([path] for path in paths.values())
            total += len(paths) if manifest.get('stopped') else max(planned, len(paths))
        return completed, None if unknown else total, 'calls' if stage == 'generate' else 'files'
    if stage in ('survive', 'cover'):
        from .adapters.java_maven import JavaMavenAdapter
        from .rules.commits import skip_reason
        adapter = JavaMavenAdapter(exp, config)
        provenance = adapter.provenance()
        filename = 'commits.csv' if stage == 'survive' else 'coverage-commits.csv'
        rows = csv_rows(exp/'results'/filename)
        frozen = read_json(exp/'results/frozen-denominators.json', {})
        groups = list(frozen)
        alive = read_json(exp/'results/alive-entering.json', {})
        changes = read_json(exp/'results/commit-paths.json', {})
        fingerprints = {}
        if stage == 'survive':
            derived = {g['name'] for g in config['dev']['groups'] if g.get('derive_from')}
            groups = [g for g in groups if g not in derived]
        for row in rows:
            index = row['index']
            build = read_json(exp/f'results/builds/{index}.json', {})
            if build.get('build') == 'fail':
                completed.append([])
                continue
            paths = []
            commit = read_json(exp/'results/anchor.json', {}) if index == '0' else build
            skipped = stage == 'survive' and index in changes and skip_reason(changes[index], config['project']['module'], config['range']['skip_untouched'])
            for group in groups:
                folder = 'raw' if stage == 'survive' else 'raw-coverage'
                path = exp/f'results/{folder}/{index}-{group}.json'
                raw = read_json(path, {})
                if skipped:
                    if raw.get('status') == 'skipped-untouched' and raw.get('sha') == commit.get('sha'):
                        paths.append(path)
                    continue
                if stage == 'cover' and group not in alive.get(index, {}):
                    continue
                selected = set(alive[index][group]) if stage == 'cover' else None
                methods = [m for m in frozen[group] if selected is None or m['id'] in selected]
                key = (group, tuple(m['id'] for m in methods))
                if key not in fingerprints:
                    fingerprints[key] = adapter.run_fingerprint(methods)
                valid = (raw.get('input_sha256') == fingerprints[key] and raw.get('runner_sha256') == provenance
                         and bool(commit.get('sha')) and raw.get('sha') == commit['sha'])
                if stage == 'cover':
                    valid = valid and raw.get('status') == 'evaluated' and (exp/f'results/coverage/{index}-{group}-lines.csv').exists()
                if valid:
                    paths.append(path)
            if groups and len(paths) == len(groups):
                completed.append(paths)
        total = len(rows) if (exp/'results'/filename).exists() else None
        return completed, total, 'commits' if stage == 'survive' else 'indices'
    return ([[]] if state.get('status') == 'completed' else []), 1, 'items'


def describe(exp, stage, config, *, at=None):
    exp = Path(exp)
    state = read_json(exp/'state'/f'{stage}.json', {})
    at = at or datetime.now(timezone.utc)
    start = datetime.fromisoformat(state['started']) if state.get('started') else None
    finish = datetime.fromisoformat(state['finished']) if state.get('finished') else None
    elapsed = max(0, ((finish or at)-start).total_seconds()) if start else 0
    try:
        completed, total, unit = items(exp, stage, config, state)
        # Cached evidence contributes to done/total, but not this attempt's rate.
        fresh = sum(bool(paths) and max(p.stat().st_mtime for p in paths) >= start.timestamp()
                    for paths in completed) if start else 0
    except (OSError, ValueError, KeyError, TypeError):
        # A live worker may be replacing a record. Reporting must not fail work.
        completed, total, unit, fresh = [], None, 'items', 0
    done = len(completed)
    eta = 'unknown'
    if state.get('status') == 'completed' and finish:
        eta = finish.isoformat(timespec='seconds')
    elif state.get('status') == 'running' and fresh and elapsed and total is not None:
        eta = (at+timedelta(seconds=elapsed*max(0, total-done)/fresh)).isoformat(timespec='seconds')
    hours, remainder = divmod(int(elapsed), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'elapsed {hours:02}:{minutes:02}:{seconds:02}; {done}/{total if total is not None else "?"} {unit}; estimated finish {eta}'
