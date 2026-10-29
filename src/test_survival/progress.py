"""Read-only stage progress from existing state and evidence records."""
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path

from .config import range_fingerprint
from .display import UTC_DISPLAY
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
        from .adapters.java_maven import JavaMavenAdapter
        from .rules.commits import skip_reason
        adapter = JavaMavenAdapter(exp, config)
        resolution_path = exp/'state/resolved-range.json'
        resolution = read_json(resolution_path, {})
        resolution_hash = range_fingerprint(config)
        plan_paths = [exp/'results'/name for name in ('anchor.json', 'commits.csv', 'commit-paths.json')]
        # Prepare publishes these inputs after resolving the current range.
        # Until that finishes, old indices cannot describe the new work safely.
        if (resolution.get('config_hash') != resolution_hash or
                any(not p.exists() or p.stat().st_mtime_ns < resolution_path.stat().st_mtime_ns for p in plan_paths)):
            return [], None, 'builds'
        anchor = read_json(exp/'results/anchor.json', {})
        commits = csv_rows(exp/'results/commits.csv')
        changes = read_json(exp/'results/commit-paths.json', {})
        total = len(commits)+1
        started = datetime.fromisoformat(state['started']).timestamp() if state.get('started') else None
        for row in [dict(index='0', sha=anchor.get('sha')), *commits]:
            index, sha = row['index'], row.get('sha')
            path = exp/f'results/builds/{index}.json'
            record = read_json(path, {})
            if not sha or record.get('sha') != sha:
                continue
            reason = skip_reason(changes[index], config['project']['module'], config['range']['skip_untouched']) if index != '0' and index in changes else None
            if reason:
                if record.get('build') == 'skipped-untouched' and record.get('reason') == reason:
                    completed.append([path])
                continue
            if record.get('input_sha256') not in adapter.build_fingerprints(sha):
                continue
            fresh = started is not None and path.stat().st_mtime >= started
            cached = record.get('build') == 'ok' and (adapter.folder(index)/'target/classes').exists()
            if record.get('build') in ('ok', 'fail') and (fresh or cached):
                completed.append([path])
        return completed, total, 'builds'
    if stage in ('generate', 'screen'):
        # Import locally: importer uses pipeline logging.
        from .importer import rounds
        total = 0
        unknown = False
        scope = read_json(exp/'population/scope.json', {})
        if stage == 'screen':
            from .adapters.java_maven import JavaMavenAdapter
            adapter = JavaMavenAdapter(exp, config)
            provenance = adapter.provenance()
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
                        record = read_json(path, {})
                        fingerprint = adapter.screen_fingerprint(source, config['screen']['stable_runs'], provenance=provenance)
                        if record.get('file') == str(source.relative_to(exp)) and record.get('input_sha256') == fingerprint:
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
        from .dev_results import imported_file_valid, load_provenance
        imported_files = load_provenance(exp).get('files', {})
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
                    fingerprints[key] = adapter.run_fingerprints(methods)
                valid = ((imported_file_valid(exp, str(path.relative_to(exp)), imported_files) or (raw.get('input_sha256') in fingerprints[key] and raw.get('runner_sha256') == provenance))
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


@dataclass(frozen=True)
class Description:
    prefix: str
    eta: str

    def render(self, display=UTC_DISPLAY):
        return self.prefix + display.timestamp(self.eta, timespec='seconds')


def description(exp, stage, config, *, at=None):
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
    return Description(f'elapsed {hours:02}:{minutes:02}:{seconds:02}; {done}/{total if total is not None else "?"} {unit}; estimated finish ', eta)


def describe(exp, stage, config, *, at=None, display=UTC_DISPLAY):
    return description(exp, stage, config, at=at).render(display)


UNITS = dict(prepare='builds', scope='items', generate='methods', screen='files',
             control='methods', survive='commits', cover='indices', report='tables')


def duration(seconds):
    if seconds is None:
        return '?'
    hours, remainder = divmod(int(max(0, seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}'


def generation_progress(exp, config, state):
    """Count focal methods, never framework files or adaptive repair calls.

    A method is counted once per planned generation (round/prompt). KTester
    gencode_response is written after insertion of generated cases; time records
    and wrapper repair records also attest completion. Archive copies share keys.
    """
    from .importer import rounds
    exp = Path(exp)
    scope = read_json(exp/'population/scope.json', {})
    population = csv_rows(exp/'population/ktester-population.csv')
    completed, calls, responses, repairs = {}, {}, set(), set()
    total, known, repair_unknown = 0, True, False
    for group in config['generators']:
        name = group['name']
        if state.get('group') not in (None, name):
            continue
        root = exp/'generated'/name
        manifest = read_json(root/'rounds.json', {})
        bases = (exp/'work/ktester'/name/'rounds', root) if group['method'] == 'ktester' else (root,)
        directories = {}
        for base in bases:
            for directory in rounds(base):
                directories.setdefault(directory.name, []).append(directory)
        count = len(directories) if manifest.get('stopped') else max(group['rounds']['max'], len(directories))
        if group['method'] == 'ktester':
            methods = len(population) if population else scope.get('m')
            if methods is None:
                known = False
            else:
                total += methods*count
            for label, copies in directories.items():
                for directory in copies:
                    for path in directory.glob('raw-responses/*/call.json'):
                        if read_json(path, {}).get('status'):
                            calls[(name, label, path.parent.name)] = path
                    for path in directory.glob('raw-responses/*/response.json'):
                        responses.add((name, label, path.parent.name))
                    for path in directory.glob('*/responses/*/gencode_response.md'):
                        completed[(name, label, path.parent.name)] = [path]
                    timing = directory/'time/time_records.json'
                    for ident, record in read_json(timing, {}).get('details', {}).items():
                        if any(k in record for k in ('process_case_response', 'process_test_response', 'check_test_class')):
                            completed[(name, label, ident)] = [timing]
                    active = []
                    repair_records = list(directory.glob('progress/*.json'))
                    for path in repair_records:
                        record = read_json(path, {})
                        if record.get('status') == 'completed':
                            completed[(name, label, path.stem)] = [path]
                        elif record.get('status') == 'running':
                            active.append(record.get('class', path.stem))
                    if not repair_records and not (directory/'run.json').exists():
                        # Legacy logs have repair starts but no per-class finish
                        # events. They cannot establish which class is active.
                        repair_unknown = True
                    repairs.update(active)
        else:
            classes = {row['class'].rsplit('.', 1)[-1]: row.get('m', 0) for row in scope.get('classes', [])}
            if not classes or not all(classes.values()):
                # Full method inventory is available even with a minimal scope record.
                inventory = csv_rows(exp/'population/methods.csv')
                for row in inventory:
                    key = row['class'].rsplit('.', 1)[-1]
                    if not classes.get(key):
                        classes[key] = sum(r['class'] == row['class'] for r in inventory)
            if not classes or not all(classes.values()):
                known = False
            total += sum(classes.values())*len(group['params']['prompts'])*count
            for label, copies in directories.items():
                for directory in copies:
                    for path in directory.glob('runs/*/*/**/run.json'):
                        if read_json(path, {}).get('status'):
                            calls[(name, label, str(path.relative_to(directory)))] = path
                    for path in directory.glob('records/*.json'):
                        record = read_json(path, {})
                        if record.get('status'):
                            for method in range(classes.get(record['class_name'], 0)):
                                completed[(name, label, record['class_name'], record['technique'], method)] = [path]
    return dict(completed=list(completed.values()), total=total if known else None,
                calls=len(calls), responses=len(responses),
                repair=', '.join(sorted(repairs)) if repairs else None if repair_unknown else '')


def panel_items(exp, stage, config, state):
    if stage == 'generate':
        p = generation_progress(exp, config, state)
        return p['completed'], p['total'], UNITS[stage], p
    if stage == 'control':
        from .dev_results import imported_file_valid, load_provenance
        imported_files = load_provenance(exp).get('files', {})
        from .adapters.java_maven import JavaMavenAdapter
        adapter = JavaMavenAdapter(exp, config)
        frozen = read_json(exp/'results/frozen-denominators.json')
        completed = []
        provenance = adapter.provenance()
        anchor = read_json(exp/'results/anchor.json', {}).get('sha')
        for group, methods in (frozen or {}).items():
            path = exp/f'results/control/0-{group}.json'
            record = read_json(path, {})
            if ((imported_file_valid(exp, str(path.relative_to(exp)), imported_files) or
                 (record.get('input_sha256') in adapter.run_fingerprints(methods)
                  and record.get('runner_sha256') == provenance))
                    and record.get('sha') == anchor):
                completed.extend([path] for _ in record.get('methods', []))
        return completed, sum(map(len, frozen.values())) if frozen is not None else None, 'methods', {}
    if stage == 'report':
        names = ('table1-survival-by-event.md', 'table2-breaking-commits.md',
                 'table3-changed-line-coverage.md', 'table4-exposure-vs-break.md', 'coverage-t.md')
        start = datetime.fromisoformat(state['started']).timestamp() if state.get('started') else float('inf')
        completed = [[p] for name in names if (p := exp/'results'/name).exists() and p.stat().st_mtime >= start]
        return completed, len(names), 'tables', {}
    completed, total, unit = items(exp, stage, config, state)
    return completed, total, unit, {}


def snapshot(exp, stage, config, *, at=None):
    """Panel metrics are separate from describe's established plain log format."""
    exp = Path(exp)
    at = at or datetime.now(timezone.utc)
    state = read_json(exp/'state'/f'{stage}.json', {})
    start = datetime.fromisoformat(state['started']) if state.get('started') else None
    finish = datetime.fromisoformat(state['finished']) if state.get('finished') else None
    elapsed = max(0, ((finish or at)-start).total_seconds()) if start else 0
    try:
        completed, total, unit, extra = panel_items(exp, stage, config, state)
        fresh = sum(bool(paths) and max(p.stat().st_mtime for p in paths) >= start.timestamp()
                    for paths in completed) if start else 0
    except (OSError, ValueError, KeyError, TypeError):
        completed, total, unit, extra, fresh = [], None, UNITS[stage], {}, 0
    done = len(completed)
    remaining = None
    eta = 'unknown'
    if state.get('status') == 'completed' and finish:
        remaining, eta = 0, finish.isoformat(timespec='seconds')
    elif state.get('status') == 'running' and fresh and elapsed and total is not None:
        remaining = elapsed*max(0, total-done)/fresh
        eta = (at+timedelta(seconds=remaining)).isoformat(timespec='seconds')
    return dict(done=done, total=total, unit=unit, elapsed=elapsed, remaining=remaining,
                eta=eta, status=state.get('status', 'pending'),
                **{k: v for k, v in extra.items() if k not in ('completed', 'total')})
