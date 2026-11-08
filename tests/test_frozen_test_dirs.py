import pytest

from test_survival.adapters import java_worker
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
