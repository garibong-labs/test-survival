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
