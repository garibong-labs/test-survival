import pytest

from test_survival.config import canonical_hash
from test_survival.policy.artifacts import frozen_test_dirs, resource_tree_hash, tree_hash


def tree(root, names):
    for name in names:
        (root/name).mkdir(parents=True)
        (root/name/'value').write_text(name)
    return root


def test_lists_every_folder_except_java_sources(tmp_path):
    root = tree(tmp_path/'src-test-t', ['java', 'resources', 'input', 'witness'])
    (root/'loopfs.sh').write_text('#!/bin/sh')
    from test_survival.adapters import java_worker
    assert frozen_test_dirs(root) == ['input', 'resources', 'suite', 'witness']
    assert java_worker.frozen_test_dirs(root) == frozen_test_dirs(root)


def test_trees_with_only_resources_and_suite_keep_their_hash(tmp_path):
    root = tree(tmp_path/'src-test-t', ['java', 'resources', 'suite'])
    earlier = canonical_hash({name: tree_hash(root/name) for name in ('resources', 'suite')})
    assert resource_tree_hash(root) == earlier


def test_other_test_data_folders_change_the_hash(tmp_path):
    root = tree(tmp_path/'src-test-t', ['java', 'resources'])
    before = resource_tree_hash(root)
    tree(root, ['input'])
    assert resource_tree_hash(root) != before


@pytest.mark.parametrize('module', ['.', 'logback-core'])
def test_records_before_the_test_data_change_stay_reusable(tmp_path, module):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.adapters.run_reuse import PRE_REACTOR, PRE_TEST_DATA, survival_provenance_matches
    from test_survival.config import validate
    adapter = JavaMavenAdapter(tmp_path, validate({'name': 'x', 'anchor': 't', 'project': {'repo': '/repo', 'module': module}}))
    current = adapter.provenance()
    assert survival_provenance_matches({**current, **PRE_TEST_DATA}, current)
    # Every component this change touched differs from its main fa45856 hash.
    assert set(PRE_TEST_DATA) == {'java_maven.py', 'java_worker.py', 'policy_coverage.py', 'PerTestCoverage.java'}
    assert all(current[name] != value for name, value in PRE_TEST_DATA.items())
    assert not survival_provenance_matches({**current, **PRE_TEST_DATA, 'PerTestCoverage.java': 'other'}, current)
    # Main 846e056 records combine both eras; they never carried the reactor key.
    earlier = {**{k: v for k, v in current.items() if k != 'reactor_module'}, **PRE_TEST_DATA, **PRE_REACTOR}
    assert survival_provenance_matches(earlier, current) == (module == '.')
    source = tmp_path/'Generated.java'; source.write_text('class Generated {}')
    keys = adapter.screen_fingerprints(source, 1)
    assert keys[0] == adapter.screen_fingerprint(source, 1)
    assert adapter.screen_fingerprint(source, 1, provenance={**current, **PRE_TEST_DATA}) in keys
    assert len(keys) == len(set(keys))


def test_trees_with_other_data_folders_do_not_reuse_pre_change_records(tmp_path):
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.adapters.run_reuse import PRE_TEST_DATA, survival_provenance_matches
    from test_survival.config import validate
    adapter = JavaMavenAdapter(tmp_path, validate({'name': 'x', 'anchor': 't', 'project': {'repo': '/repo'}}))
    tree(tmp_path/'dev/src-test-t', ['java', 'resources', 'input'])
    current = adapter.provenance()
    assert current['test_data_dirs'] == 'input'
    # A main fa45856 record never carried the key: its runtime lacked src/test/input.
    earlier = {**{k: v for k, v in current.items() if k != 'test_data_dirs'}, **PRE_TEST_DATA}
    assert not survival_provenance_matches(earlier, current)
    source = tmp_path/'Generated.java'; source.write_text('class Generated {}')
    assert adapter.screen_fingerprint(source, 1, provenance=earlier) not in adapter.screen_fingerprints(source, 1)


def test_concurrent_leases_restore_only_dirty_files(tmp_path):
    from test_survival.adapters.test_data_runtime import reset_runtime, runtime_slot
    root = tree(tmp_path/'frozen', ['input', 'resources'])
    pool = tmp_path/'slots'
    with runtime_slot(pool) as first:
        reset_runtime(root, first)
        unchanged = first/'src/test/input/value'
        unchanged_signature = unchanged.stat()
        (first/'src/test/resources/value').write_text('damaged')
        (first/'src/test/resources/added').touch()
        (first/'outside').write_text('also private')
        with runtime_slot(pool) as second:
            reset_runtime(root, second)
            assert first != second
            assert (second/'src/test/resources/value').read_text() == 'resources'
            assert not (second/'src/test/resources/added').exists()
    with runtime_slot(pool) as again:
        assert again == first
        reset_runtime(root, again)
        assert (again/'src/test/resources/value').read_text() == 'resources'
        assert not (again/'src/test/resources/added').exists()
        assert not (again/'outside').exists()
        assert unchanged.stat().st_ino == unchanged_signature.st_ino
        assert unchanged.stat().st_ctime_ns == unchanged_signature.st_ctime_ns
    assert (root/'resources/value').read_text() == 'resources'


def test_first_catalog_rejects_conflicting_legacy_evidence(tmp_path):
    from test_survival.frozen_tree import frozen_resource_hash
    from test_survival.io import write_json
    root = tree(tmp_path/'dev/src-test-t', ['resources'])
    write_json(tmp_path/'results/baseline-batches/0.json',
               {'methods':[{'resource_tree_hash':resource_tree_hash(root)}]})
    (root/'resources/value').write_text('modified before upgrade')
    with pytest.raises(RuntimeError, match='differs from its recorded resource_tree_hash'):
        frozen_resource_hash(root)
    assert not (root.parent/'frozen-test-tree.json').exists()


def test_catalog_pins_an_empty_resource_tree(tmp_path):
    from test_survival.frozen_tree import frozen_resource_hash
    root = tmp_path/'dev/src-test-t'
    empty = frozen_resource_hash(root)
    assert frozen_resource_hash(root) == empty
    tree(root, ['resources'])
    with pytest.raises(RuntimeError, match='added resources/value'):
        frozen_resource_hash(root)
