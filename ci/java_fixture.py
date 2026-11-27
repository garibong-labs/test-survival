"""Prepare checksum-pinned Java fixtures and reject skipped integration reports."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import xml.etree.ElementTree as ET


MANIFEST = Path(__file__).resolve().parents[1]/'src/test_survival/adapters/java/classpath-artifacts.json'
FIXTURES = {
    'test_testpilot_source_materials',
    'test_testpilot_junit4_policy_pool',
    'test_batch_loader_remains_open_until_group_ends',
    'test_mockito_internal_and_batch_classpaths[assertj]',
    'test_mockito_internal_and_batch_classpaths[project-mockito]',
    'test_snapshot_compiler_drains_late_diagnostics',
    'test_candidate_coverage_preserves_isolated_setup_and_initializer_lines',
    'test_real_iterative_compilation_and_dependency_graph',
    'test_broad_runtime_hits_parallel_chunks_and_memory',
    'test_nested_shared_setup_dependencies_select_every_descendant',
    'test_lazy_initializer_dependencies_survive_probe_resets[test]',
    'test_lazy_initializer_dependencies_survive_probe_resets[production]',
    'test_lazy_initializer_dependencies_survive_probe_resets[failed]',
    'test_lazy_initializer_dependencies_survive_probe_resets[isolated]',
    'test_lazy_initializer_dependencies_survive_probe_resets[defined]',
    'test_scope_universe_without_tests_or_probe_data',
    'test_production_worker_includes_new_package_and_excludes_generated',
    'test_dependency_identity_with_real_runner[image]',
    'test_dependency_identity_with_real_runner[compose]',
    'test_container_owned_scratch_cleanup',
    'test_inventory_records_sealed_annotations_and_multiline',
    'test_inventory_skips_bad_type_without_partial_rows',
    'test_imports_resolve_in_the_declaring_source_file',
    'test_policy_coverage_does_not_assign_container_teardown_to_next_test[jupiter]',
    'test_policy_coverage_does_not_assign_container_teardown_to_next_test[vintage]',
    'test_mixed_snapshot_batch_switches_every_test_data_folder',
    'test_survive_isolates_frozen_tree_across_commits',
    'test_policy_per_test_coverage_one_jvm_with_vintage_and_source_versions',
    'test_inherited_identity_survives_concrete_execution_and_batched_coverage[junit4]',
    'test_inherited_identity_survives_concrete_execution_and_batched_coverage[junit5]',
    'test_junit4_inventory_selectors_and_vintage_launcher',
    'test_real_worker_environment_retries_and_single_method_coverage',
    'test_real_compiler_session_equivalence',
    'test_real_group_reuse_preserves_retry_and_coverage',
    'test_real_parallel_compiler_sessions_are_bounded_and_isolated',
}


# Maven Central occasionally answers CI runners with 429 or 5xx, and runner
# networking can fail transiently. Other HTTP errors (for example 404) fail fast.
ATTEMPTS = 5
BACKOFF_SECONDS = 5
RETRY_STATUS = {429, 500, 502, 503, 504}


def retry_delay(error, attempt):
    delay = BACKOFF_SECONDS * 2 ** (attempt - 1)
    if isinstance(error, HTTPError) and error.headers is not None:
        try:
            delay = max(delay, min(float(error.headers.get('Retry-After', '')), 120))
        except ValueError:
            pass
    return delay


def download(url, stream, *, attempts=ATTEMPTS, sleep=None):
    """Copy url into stream, retrying transient network and server errors."""
    for attempt in range(1, attempts + 1):
        stream.seek(0)
        stream.truncate()
        try:
            with urlopen(url, timeout=60) as response:
                shutil.copyfileobj(response, stream)
            stream.flush()
            return
        except HTTPError as error:
            if error.code not in RETRY_STATUS or attempt == attempts:
                raise
            last = error
        except (URLError, OSError) as error:
            if attempt == attempts:
                raise
            last = error
        delay = retry_delay(last, attempt)
        print(f'Download failed ({last}); retry {attempt}/{attempts - 1} in {delay:g}s: {url}', file=sys.stderr)
        (sleep or time.sleep)(delay)


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
                download(url, stream)
                if checksum(temporary) != record['sha256']:
                    raise ValueError('Checksum mismatch: '+name)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    print(f'Verified {len(records)} fixture libraries against {MANIFEST.name}')


def verify_report(path):
    if not path.is_file():
        raise SystemExit(f'{path} not found: the Java fixtures did not run because an earlier step failed')
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
