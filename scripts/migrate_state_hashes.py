"""One-off migration of completed stage config_hash values to as-written hashes."""
import argparse
import json
from pathlib import Path
import re

from test_survival.config import STAGES, load, stage_hash
from test_survival.io import atomic_text, now, read_json, write_json

HASH_FIELD = re.compile(r'("config_hash"\s*:\s*")[^"\r\n]*(")')


def migrate(experiment, stages, evidence):
    experiment, evidence = Path(experiment).resolve(), Path(evidence)
    config = load(experiment)
    plans = []
    for stage in stages:
        if stage not in STAGES:
            raise ValueError('Unknown stage: '+stage)
        path = experiment/'state'/f'{stage}.json'
        original = path.read_text()
        record = json.loads(original)
        if record.get('status') != 'completed':
            raise ValueError('Only completed stage records can be migrated: '+stage)
        digest = stage_hash(config, stage)
        updated, replacements = HASH_FIELD.subn(lambda match: match[1]+digest+match[2], original)
        if replacements != 1 or json.loads(updated) != {**record, 'config_hash': digest}:
            raise ValueError('Expected exactly one config_hash field: '+stage)
        plans.append((path, original, updated, dict(stage=stage, before=record['config_hash'], after=digest, changed=original!=updated)))
    # Validate all requested records before making any write. Preserve formatting
    # and every byte outside the config_hash value, including all other fields.
    results = []
    for path, original, updated, result in plans:
        if path.read_text() != original:
            raise RuntimeError('Stage record changed during migration: '+path.stem)
        if original != updated:
            atomic_text(path, updated)
        results.append(result)
    history = read_json(evidence, {'migrations': []})
    history['migrations'].append(dict(experiment=str(experiment), timestamp=now(), stages=results))
    write_json(evidence, history)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('experiment', type=Path)
    parser.add_argument('--stages', nargs='+', choices=STAGES, required=True)
    parser.add_argument('--evidence', type=Path, default=Path(__file__).resolve().parents[1]/'tests/evidence/state-hash-migration.json')
    args = parser.parse_args()
    for result in migrate(args.experiment, args.stages, args.evidence):
        print(json.dumps(result))


if __name__ == '__main__':
    main()
