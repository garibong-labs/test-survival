"""Prepare checksum-pinned Java fixtures and reject skipped integration reports."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from urllib.request import urlopen
import xml.etree.ElementTree as ET


MANIFEST = Path(__file__).resolve().parents[1]/'src/test_survival/adapters/java/classpath-artifacts.json'
FIXTURES = {
    'test_inventory_records_sealed_annotations_and_multiline',
    'test_inventory_skips_bad_type_without_partial_rows',
    'test_policy_coverage_does_not_assign_container_teardown_to_next_test[jupiter]',
    'test_policy_coverage_does_not_assign_container_teardown_to_next_test[vintage]',
    'test_policy_per_test_coverage_one_jvm_with_vintage_and_source_versions',
    'test_junit4_inventory_selectors_and_vintage_launcher',
    'test_real_worker_environment_retries_and_single_method_coverage',
    'test_real_compiler_session_equivalence',
    'test_real_group_reuse_preserves_retry_and_coverage',
    'test_real_parallel_compiler_sessions_are_bounded_and_isolated',
}


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prepare_libraries(destination, records=None):
    destination.mkdir(parents=True, exist_ok=True)
    records = json.loads(MANIFEST.read_text()) if records is None else records
    for record in records:
        group, artifact, version = record['coordinate'].split(':')
        classifier = '-'+record['classifier'] if record.get('classifier') else ''
        name = record['filename']
        if Path(name).name != name:
            raise ValueError('Library filename must be a basename')
        target = destination/name
        if target.is_file() and checksum(target) == record['sha256']:
            continue
        url = ('https://repo.maven.apache.org/maven2/'+group.replace('.', '/')+'/'+artifact+'/'+version
               +'/'+artifact+'-'+version+classifier+'.jar')
        with tempfile.NamedTemporaryFile(dir=destination, suffix='.download', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                with urlopen(url, timeout=60) as response:
                    shutil.copyfileobj(response, stream)
                stream.flush()
                if checksum(temporary) != record['sha256']:
                    raise ValueError('Checksum mismatch: '+name)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    print(f'Verified {len(records)} fixture libraries against {MANIFEST.name}')


def verify_report(path):
    cases = list(ET.parse(path).getroot().iter('testcase'))
    if len(cases) != len(FIXTURES) or {case.get('name') for case in cases} != FIXTURES:
        raise ValueError('Expected all real Java fixtures in the report')
    if any(case.find(kind) is not None for case in cases for kind in ('skipped', 'failure', 'error')):
        raise ValueError('Real Java fixtures must pass without skips, failures, or errors')
    print('All real Java fixtures passed; zero skips, failures, or errors')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['libraries', 'report'])
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    if args.action == 'libraries':
        prepare_libraries(args.path)
    else:
        verify_report(args.path)


if __name__ == '__main__':
    main()
