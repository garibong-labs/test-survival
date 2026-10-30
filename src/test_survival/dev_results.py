"""Portable, checksummed developer measurements for repeated experiments."""
from functools import lru_cache
import hashlib
from pathlib import Path
import re
import shutil

import yaml

from .config import MISSING, ConfigError, UniqueLoader, canonical_hash, runner_inputs, validate, execution_groups
from .io import now, read_csv, read_json, write_json
from .pipeline import PipelineError, invalidate
from .rules.dev_denominator import denominator_file, unmapped_note


UNMAPPED_AUDIT = 'dev/dev-unmapped-excluded.csv'


def refresh_unmapped_metadata(exp):
    """Keep disclosures current even when an import reuses completed prepare."""
    anchor = read_json(exp/'results/anchor.json')
    if anchor is None:
        return ''
    old_note = unmapped_note(anchor.get('dev_unmapped_excluded', 0))
    path = exp/UNMAPPED_AUDIT
    anchor['dev_unmapped_excluded'] = len(read_csv(path)) if path.exists() else 0
    note = unmapped_note(anchor['dev_unmapped_excluded'])
    anchor['deviations'] = [value for value in anchor.get('deviations', []) if value != old_note and value != note]
    if note:
        anchor['deviations'].append(note)
    write_json(exp/'results/anchor.json', anchor)
    return note


def check_unmapped_audit(exp, record):
    if UNMAPPED_AUDIT in record['files']:
        require_record(exp, record, UNMAPPED_AUDIT)
    elif (exp/UNMAPPED_AUDIT).exists():
        raise PipelineError('Imported baseline has an unrecorded exclusion audit; reimport the baseline')


@lru_cache(maxsize=16384)
def _file_hash(path, device, inode, size, mtime_ns, ctime_ns):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_hash(path):
    """Reuse digests only while the file identity and change metadata match."""
    path = Path(path).resolve()
    stat = path.stat()
    return _file_hash(path, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def load_provenance(exp):
    return read_json(exp/'state/import.json', {}).get('dev-results', {})


def archived_config(source):
    """Ignore obsolete generation/report options, but validate measurement inputs."""
    try:
        raw = yaml.load((source/'experiment.yaml').read_text(), Loader=UniqueLoader)
        selected = {k: raw[k] for k in ('project', 'runner', 'survival', 'coverage', 'dev') if k in raw}
        selected.update(name='dev-results-source', anchor=raw.get('anchor', 'archived'))
        selected['range'] = {'skip_untouched': raw.get('range', {}).get('skip_untouched', 'none')}
        # This removed option controlled cache use, not measured outcomes.
        if 'survival' in selected:
            selected['survival'] = {k: v for k, v in selected['survival'].items() if k != 'dev_cache'}
        return validate(selected)
    except yaml.YAMLError as exc:
        raise PipelineError('dev-results source compatibility configuration is invalid YAML') from exc
    except (ConfigError, AttributeError, TypeError) as exc:
        raise PipelineError(f'dev-results source compatibility configuration is invalid: {exc}') from exc


def tree_hash(root):
    if not root.is_dir():
        raise PipelineError(f'dev-results requires the frozen test tree: {root}')
    return canonical_hash({str(p.relative_to(root)): file_hash(p)
                           for p in sorted(root.rglob('*')) if p.is_file()})


def identity(exp, config):
    anchor = read_json(exp/'results/anchor.json', {})
    commits_path = exp/'results/commits.csv'
    if not anchor.get('sha') or not commits_path.exists():
        raise PipelineError(f'dev-results requires prepared anchor and commits: {exp}')
    commits = read_csv(commits_path)
    if any(not row.get('sha') or row.get('index') != str(i) for i, row in enumerate(commits, 1)):
        raise PipelineError(f'dev-results requires an indexed commit sha sequence: {commits_path}')
    return dict(project_repo=str(Path(config['project']['repo']).resolve()), anchor_sha=anchor['sha'],
                commit_shas=[row['sha'] for row in commits], skip_untouched=config['range']['skip_untouched'],
                dev_tree_sha256=tree_hash(exp/'dev/src-test-t'),
                measurement_settings=dict(project={k: v for k, v in config['project'].items() if k != 'repo'},
                                          runner=runner_inputs(config['runner']), survival=config['survival'],
                                          stable_runs=config['dev']['stable_runs'], unmapped=config['dev']['unmapped'],
                                          coverage=config['coverage']))


DETACH = '; remove the dev-results entry from state/import.json to measure these groups afresh'


def _divergence(actual, expected, prefix=''):
    """Dotted path of the first differing leaf, so refusals name the setting."""
    if isinstance(actual, dict) and isinstance(expected, dict):
        for key in sorted(set(actual) | set(expected), key=str):
            if actual.get(key, MISSING) != expected.get(key, MISSING):
                return _divergence(actual.get(key), expected.get(key), f'{prefix}{key}.')
    return prefix.rstrip('.')


def check_identity(actual, expected, *, advice=''):
    # Manifests written before dev.unmapped used the default error policy.
    actual = {**actual, 'measurement_settings': {'unmapped': 'error', **actual.get('measurement_settings', {})}}
    expected = {**expected, 'measurement_settings': {'unmapped': 'error', **expected.get('measurement_settings', {})}}
    labels = dict(project_repo='project repo', anchor_sha='anchor sha', commit_shas='commit sha sequence',
                  skip_untouched='skip_untouched', dev_tree_sha256='dev/src-test-t tree hash',
                  measurement_settings='result-affecting measurement settings')
    for key, label in labels.items():
        if actual.get(key) != expected.get(key):
            detail = _divergence(actual.get(key), expected.get(key)) if key == 'measurement_settings' else ''
            raise PipelineError(f'dev-results refused: {label}{f" ({detail})" if detail else ""} differs{advice}')


def import_results(exp, source, config):
    """Preflight all compatibility checks and files before copying any evidence."""
    from .stages.survive import raw_folder
    source_config = archived_config(source)
    source_provenance = load_provenance(source)
    source_identity = identity(source, source_config)
    check_identity(identity(exp, config), source_identity)
    source_groups = {g['name']: (i, g) for i, g in enumerate(source_config['dev']['groups'])}
    matched = [(i, g) for i, g in enumerate(config['dev']['groups']) if g['name'] in source_groups
               and g['denominator'] == source_groups[g['name']][1]['denominator']]
    if not matched:
        raise PipelineError('dev-results: no Dev groups have matching names and denominator rules')
    frozen = read_json(source/'results/frozen-denominators.json', {})
    plan, groups = {}, {}
    # Shared baselines/sources are portable. Group-specific denominators must be
    # mapped by name, since the first group's filename is always dev-denominator.
    for path in sorted((source/'dev').rglob('*')):
        relative = str(path.relative_to(source))
        if path.is_file() and (relative == UNMAPPED_AUDIT or not path.name.endswith(('-denominator.csv', '-excluded.csv'))):
            plan[relative] = path
    for index, group in matched:
        name = group['name']
        source_index, old = source_groups[name]
        if name not in frozen:
            raise PipelineError(f'dev-results: missing frozen denominator for {name} in {source}')
        plan[denominator_file(group, index)] = source/denominator_file(old, source_index)
        excluded = source/f'dev/{name}-excluded.csv'
        if excluded.exists():
            plan[str(excluded.relative_to(source))] = excluded
        group_paths = []
        pattern = re.compile(r'\d+-'+re.escape(name)+r'(?:\.[^/]+|-lines\.csv)$')
        for folder in ('raw', 'raw-derived', 'coverage', 'raw-coverage', 'control'):
            for path in sorted((source/'results'/folder).glob('*')):
                if path.is_file() and pattern.fullmatch(path.name):
                    relative = str(path.relative_to(source))
                    plan[relative] = path
                    group_paths.append(relative)
        # Measured/derived is an execution choice. Reuse the same observations
        # even if the destination represents this denominator differently.
        source_folder = raw_folder(name, source_config, source_provenance)
        target_folder = raw_folder(name, config)
        if source_folder != target_folder:
            for position, relative in enumerate(group_paths):
                if relative.startswith(f'results/{source_folder}/'):
                    target = relative.replace(f'results/{source_folder}/', f'results/{target_folder}/', 1)
                    plan[target] = plan.pop(relative)
                    group_paths[position] = target
        control = f'results/control/0-{name}.json'
        if control not in plan:
            raise PipelineError(f'dev-results: missing control result for {name} in {source}')
        groups[name] = dict(denominator=group['denominator'], frozen=frozen[name], files=group_paths, raw_folder=target_folder)
    for target, path in plan.items():
        if not path.is_file():
            raise PipelineError(f'dev-results: missing source file {path}')
    hashes = {}
    for target, path in plan.items():
        destination = exp/target
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Identical frozen source files need no rewrite. Their retained bytes
        # are still included in the manifest, just like copied baseline files.
        if not target.startswith('dev/src-test-t/'):
            shutil.copy2(path, destination)
        hashes[target] = file_hash(destination)
    if UNMAPPED_AUDIT not in plan:
        (exp/UNMAPPED_AUDIT).unlink(missing_ok=True)
    refresh_unmapped_metadata(exp)
    destination_frozen = read_json(exp/'results/frozen-denominators.json', {})
    destination_frozen.update({name: record['frozen'] for name, record in groups.items()})
    write_json(exp/'results/frozen-denominators.json', destination_frozen)
    counts = read_json(exp/'results/denominators.json', {})
    counts.update({name: len(record['frozen']) for name, record in groups.items()})
    write_json(exp/'results/denominators.json', counts)
    # Baselines/populations feed scope selection and LLM subsampling/funnels.
    # Keep generated call evidence, but rebuild all dependent stage summaries.
    invalidate(exp, 'prepare')
    prepare_path = exp/'state/prepare.json'
    prepare = read_json(prepare_path, {})
    prepare['dev_baseline'] = dict(status='completed', source=str(source),
                                  source_hash=canonical_hash({p: h for p, h in hashes.items() if p.startswith('dev/')}))
    write_json(prepare_path, prepare)
    return dict(source=str(source), imported=now(), compatibility=source_identity, groups=groups,
                files=hashes, source_files={target: str(path.relative_to(source)) for target, path in plan.items()},
                source_sha256=canonical_hash(hashes))


def imported_groups(exp, config, stage=None, *, provenance=None):
    """Validate a saved import before bypassing measurement or cache fingerprints."""
    record = load_provenance(exp) if provenance is None else provenance
    if not record:
        return {}
    current = {g['name']: g for g in config['dev']['groups']}
    groups = {name: item for name, item in record['groups'].items() if name in current}
    if stage:
        groups = execution_groups(config, groups)
    if not groups:
        return {}
    check_identity(identity(exp, config), record['compatibility'], advice=DETACH)
    check_unmapped_audit(exp, record)
    frozen = read_json(exp/'results/frozen-denominators.json', {})
    for name, item in groups.items():
        if current[name]['denominator'] != item['denominator']:
            raise PipelineError(f'dev-results refused: denominator rule changed for {name}')
        if stage and frozen.get(name) != item['frozen']:
            raise PipelineError(f'dev-results refused: frozen denominator changed for {name}; rerun screen')
        if stage:
            folders = {'control': ('control',), 'survive': ('raw', 'raw-derived'),
                       'cover': ('raw-coverage', 'coverage')}[stage]
            for path in item['files']:
                if Path(path).parts[1] in folders:
                    require_record(exp, record, path)
    return groups


def imported_for(exp, config, stage):
    """Provenance plus the validated imported groups a measurement stage may skip."""
    provenance = load_provenance(exp)
    return provenance, imported_groups(exp, config, stage, provenance=provenance)


def require_record(exp, provenance, relative):
    path = exp/relative
    expected = provenance['files'].get(relative)
    if not expected or not path.is_file() or file_hash(path) != expected:
        raise PipelineError(f'dev-results refused: imported evidence missing or changed: {relative}')


def imported_record(exp, relative, *, sha=None, method_ids=None, provenance=None):
    provenance = provenance if provenance is not None else load_provenance(exp)
    require_record(exp, provenance, relative)
    record = read_json(exp/relative)
    if sha is not None and record.get('sha') != sha:
        raise PipelineError(f'dev-results refused: commit sha differs in {relative}')
    if method_ids is not None and {m['id'] for m in record.get('methods', [])} != set(method_ids):
        raise PipelineError(f'dev-results refused: method population differs in {relative}')
    return record


def imported_file_valid(exp, relative, files=None):
    """Read-only progress display may count imports despite old runner fingerprints."""
    if files is None:
        files = load_provenance(exp).get('files', {})
    expected = files.get(relative)
    return bool(expected and (exp/relative).is_file() and file_hash(exp/relative) == expected)


def imported_baseline(exp, config):
    record = load_provenance(exp)
    if not record:
        return False
    check_identity(identity(exp, config), record['compatibility'], advice=DETACH)
    require_record(exp, record, 'dev/dev-methods-all.csv')
    check_unmapped_audit(exp, record)
    return True
