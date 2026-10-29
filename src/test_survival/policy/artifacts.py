"""Source snapshots and stable replacement-unit provenance."""
import hashlib
from pathlib import Path
import shutil

from ..io import read_csv, read_json


def unit(exp, group, test, method, scope):
    if method == 'dev':
        return test['class']+'#'+test['method']
    source = Path(test['file'])
    if method == 'ktester':
        rows = read_csv(exp/'population/ktester-population.csv')
        candidates = [r for r in rows if r['test-class'].rsplit('.', 1)[-1] == source.stem]
        if len(candidates) != 1:
            raise ValueError('Cannot map KTester test to focal method: '+str(source))
        return candidates[0]['class']+'#'+candidates[0]['method-name']
    # Ouedraogo records preserve the target class separately from the generated class.
    for parent in source.parents:
        ledger = exp/parent/'generation.json'
        if ledger.exists():
            matches = [r for r in read_json(ledger) if r.get('file') == test['file']]
            if len(matches) == 1:
                simple = matches[0].get('class_name', matches[0].get('class'))
                candidates = [c['class'] for c in scope if c['class'].rsplit('.', 1)[-1] == simple]
                if len(candidates) == 1:
                    return candidates[0]
    candidates = [c['class'] for c in scope if c['class'].rsplit('.', 1)[-1] in source.parts]
    if len(candidates) != 1:
        raise ValueError('Cannot map Ouedraogo test to target class: '+str(source))
    return candidates[0]


def snapshot(source_exp, destination, tests, *, group, method, scope, prefix):
    """Copy evidence, never symlink into the read-only baseline."""
    result = []
    if method == 'dev':
        source = source_exp/'dev/src-test-t'
        target = destination/prefix/'dev/src-test-t'
        if not target.exists():
            shutil.copytree(source, target)
    else:
        for name in ('resources', 'suite'):
            source = source_exp/'dev/src-test-t'/name
            target = destination/prefix/'dev/src-test-t'/name
            if source.exists() and not target.exists():
                shutil.copytree(source, target)
    for t in tests:
        source = source_exp/t['file']
        target = destination/prefix/t['file']
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        row = {**t, 'file':str(target.relative_to(destination)), 'file_hash':digest,
               'unit':unit(source_exp, group, t, method, scope), 'origin_id':t['id'], 'resource_root':str(Path(prefix)/'dev/src-test-t')}
        if method == 'dev':
            row.update(source_root=str(Path(prefix)/'dev/src-test-t/java'), resource_root=str(Path(prefix)/'dev/src-test-t'))
            row['id'] = t['class']+'#'+t['method']
        result.append(row)
    return result
