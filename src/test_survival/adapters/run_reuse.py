"""Compatibility for measured survival records preceding execution-only changes."""
from ..io import read_json

# Historical runner provenances whose measured records stay valid. Each map
# lists EVERY file that differs from the current provenance for that era, so a
# record matches only when all other components are identical.
#
# PR #21 (0871fbb): launch orchestration and dead-method bookkeeping only.
PRE_BOUNDED_COVERAGE = {
    'java_maven.py': '56444d206dc46558a90e59e88ed948589b15ceccb85e240a808620e59a1844e1',
    'java_worker.py': '28f110f37c458d7b371a1e8ec139a4aacd088c57f56c8b4209d5e0560727cec5',
    'policy_coverage.py': '55641f372e3d7840830563d58b3d01373d809621a3ea4fb1ec948d329e3e3d2b',
    'PerTestCoverage.java': '38f5b9f652d094df030322549fda9f40a3dad1f388086f3f24b1ac4676049c83',
}
# PR #24 (2214f25): before the multi-release class-root fix. The JaCoCo report
# and PerTestCoverage analyzed target/classes as one root; records that
# succeeded had no META-INF/versions copies and are unchanged by the fix.
# Failed ones (coverage-fail) are never reused (see run_group).
PRE_MULTI_RELEASE = {
    'java_maven.py': 'c29403019713665135326de9d7c567c56ae494568542429445da0e04d55d8980',
    'java_worker.py': '18050d4c02aae2db2df912ee8ee7e685abb27f511467a9e658855092635df103',
    'PerTestCoverage.java': '38f5b9f652d094df030322549fda9f40a3dad1f388086f3f24b1ac4676049c83',
}
# Before the explicit System.exit at the end of PerTestCoverage.main: the helper only
# changed how the JVM terminates, never what it measured or wrote.
PRE_EXPLICIT_EXIT = {
    'PerTestCoverage.java': '77534f14a4fd91b9127470c258efe951be8de985d595bbb27d5b84e558fb636b',
}
# Main 846e056, before submodule commands moved to the reactor root. A root
# module (project.module: .) runs the same Maven commands with the same cache keys.
# Submodule provenances carry `reactor_module`, which no record from this era has,
# so this era never matches them.
PRE_REACTOR = {
    'java_maven.py': '23cfe6cfe3c0cc135ef236ca8796fd9568a460718e9a4d90e2c8f46b894e1e55',
    'java_worker.py': '3a8bef2cf3c2bccc09cf261f0bad7f963eab878725eb9cae213e59d82c79dbcd',
    'maven_dependencies.py': '895d95439e2c27937e14c3d78aa954570f5d5ad515de3a5859b0f53c4a3fa8dd',
    'cache.py': '9332cd7d9a8fcac8e8dd8df2572efe81c8784b3e45d5d9f0646912f498ed8147',
    'selection_context.py': 'ad251e59aeb38b02918bba70c05e46ae9b40ef32fad04c84f9456de996b76774',
}
HISTORICAL = (PRE_BOUNDED_COVERAGE, PRE_MULTI_RELEASE, PRE_EXPLICIT_EXIT, PRE_REACTOR)
# Main fa45856, before every non-java folder of the frozen src/test tree went into
# test runtimes. A tree whose only data folders are resources and suite stages and
# switches exactly the same files, so its records stay valid. A tree with other data
# folders carries `test_data_dirs` in its provenance, which no record from this era
# has, so this era never matches it. It combines with every era above: those list
# their differences from this layout.
PRE_TEST_DATA = {
    'java_maven.py': 'a8139c2f2e99a30f93071da240d5a07730016678599e37997d83ac6d37faa881',
    'java_worker.py': '4eb9e24ed984fd4dce0428cbaec32342163cefc5b2742a832e5c3a141e3d07dd',
    'policy_coverage.py': 'c7f5c2d9eee38f0d8e91dafc6e98a393895a55c1aa022d88e7415b54f92aac31',
    'PerTestCoverage.java': '5b659955ca802ce67026c091155df1f634ee8815cdb69a291cc40442e42fda2e',
}


def test_data_layouts(current):
    """The current provenance, then the same components before the test-data change."""
    if not isinstance(current, dict) or not PRE_TEST_DATA.keys() <= current.keys():
        return (current,)
    return (current, {**current, **PRE_TEST_DATA})


# Main 01ae5d0, before storage/lifecycle-only changes. The two new provenance
# entries did not exist in that layout. Keep all other components exact.
PRE_FOOTPRINT = {
    'policy_coverage.py': '4bef5ce356d3004b00d307b6c34c3e418f9ec02bedb1c4708285aee6181a6f49',
    'PerTestCoverage.java': '1e940e096effde6bdfd59ea8465ff7728537eda7460cc9b96bf9604d3f65bb76',
    'java_maven.py': 'fbe13ded0601be99ca5684d9a867e4e18f289537b6bfb40fb86dfc3eafb7182a',
    'java_worker.py': 'd62468de6dfa7897deb0bbcce4567063a7e6836494319fa6728b34653ab9475f',
    'compilation.py': 'e76f7debb3201fc4b636479a42ec0f87409061c5fda4a62a95e74a67c1c64320',
    'cache.py': '5a5396703aab5a9060b76e87ce7a459e6307d2849db770bdfc086578997526cc',
}


def pre_footprint_provenance(current):
    return {**{k:v for k,v in current.items() if k not in ('io.py', 'compile_events.py')}, **PRE_FOOTPRINT}


def _matches(previous, current):
    return any(_matches_layout(previous, layout) for layout in test_data_layouts(current))


def _matches_layout(previous, current):
    if previous == current or any(previous == {**current, **era} for era in HISTORICAL):
        return True
    if not PRE_FOOTPRINT.keys() <= current.keys():
        return False
    old = pre_footprint_provenance(current)
    return previous == old or any(previous == {**old, **era} for era in HISTORICAL)


def cached_survival_record(adapter, exp, index, group, methods):
    """Read a compatible full-denominator cache; caller validates dead omissions.

    Keeping omission validation separate lets dispatch await an earlier result
    instead of needlessly replacing a valid sequential cache with extra tests.
    """
    record = read_json(exp/f'results/raw/{index}-{group}.json', {})
    if record.get('status') != 'evaluated' or not record.get('input_sha256') or not record.get('runner_sha256'):
        return None
    commit = read_json(exp/f'results/builds/{index}.json', {})
    if (record.get('sha') != commit.get('sha')
            or {t['id'] for t in record.get('methods', [])} != {t['id'] for t in methods}
            or record['input_sha256'] not in adapter.run_fingerprints(methods)
            or not survival_provenance_matches(record['runner_sha256'], adapter.provenance())):
        return None
    return record


PRE_BATCH = {'java_maven.py': 'ee9a09ff0719b4ddef84e1197d1dc0c5d1ee6c82c1a7c4f0f469c7f17a5300f1', 'policy_coverage.py': '4bef5ce356d3004b00d307b6c34c3e418f9ec02bedb1c4708285aee6181a6f49', 'PerTestCoverage.java': '1e940e096effde6bdfd59ea8465ff7728537eda7460cc9b96bf9604d3f65bb76'}


def survival_provenance_matches(previous, current):
    # New batch behavior is opt-in. Legacy worker/selection semantics did not
    # change; compare every unrelated provenance component exactly as before.
    return _matches(previous, current) or _matches(previous, {**current, **PRE_BATCH})


# These exact revisions only add opt-in production coverage. Preserve the
# historical scope-mode record bytes. A later implementation gets its own hash.
_SCOPE_EQUIVALENT = {
    # Draining diagnostics preserves completed measurements.
    # SnapshotCompiler.java
    '2bc5f55af7dede1625f303ab4185ba1aca345d8a0834f2462f5befd56188c857':
        'ac9eb8848fbba506a124c55b999a5b9a317f3302ad8735e59c7e6bde709a2f14',
    # DependencySources.java
    '659014c72e88f5ef05f49823264d5485f243295143ac0d9224cb1a63bf121237':
        '451099988f6a247d3820ff0ca38b0ea68f01057a93e515c82e34b3236acfe836',
    # batch.py
    '6cbf7d5dcaa136f148e63dedb04f47618ab3ee4354e630e20b20dc36a1876997':
        'ca2b556f923a56a428fff5d93737df253bd8ac694cbdc9d4309988f6d7c4f86f',
    # java_maven.py
    '28f4a4ad6145ec377513bf2cb128263d71e918bd40065daba3433842d22d792d':
        'b44593dba082d9e85b86eb2c2182ef3aa363ee7ad41e26133514af2c09aa6d47',
    # policy_coverage.py
    '106ae3ffefc7036e663e346fde23bf0f0a9186615dc6658bba988ac07ffe94c7':
        '0ae99b4f0e9817de8f0fbef74d368ef22ffa729ed6be0ad834382c49550838ea',
    # PerTestCoverage.java
    '1c780521de8bce26eb012c2fd20c08c03d42b527de9b4d39ebe64e35681f43f2':
        'ded7259fc81d237334e543defc8eb5b215de0ec2e8f1229171da1ea97524156f',
}


def scope_compatible_fingerprint(digest):
    return _SCOPE_EQUIVALENT.get(digest, digest)

# Frozen-tree isolation preserves the original measurement keys for these exact
# revisions. Further changes must be evaluated independently in both universes.
_FROZEN_TREE_EQUIVALENT = {
    # java_maven.py
    '0b202e9452a6b884258a4750f22e8b399de1baa6b3b9cf8725e66d009da6637e':
        '28f4a4ad6145ec377513bf2cb128263d71e918bd40065daba3433842d22d792d',
    # java_worker.py
    '5a517fe657a1fbf2a5015371037315e50edbbf9d5d73d37cb88daf7c4a2376d8':
        '6d79a1f3858e6c19dcab5c853a31b0f26a191d7bb465536e1b5b53d114555167',
    # policy_coverage.py
    'd0ea96ea361a4aee3106accf894b4f3fad4c946da89dc48efb63d2380f12ab02':
        '106ae3ffefc7036e663e346fde23bf0f0a9186615dc6658bba988ac07ffe94c7',
    # PerTestCoverage.java
    'e20d02cdde5f910939230b2e3a12baa9e2e770e2780c73075a842c56c1f82da4':
        '1c780521de8bce26eb012c2fd20c08c03d42b527de9b4d39ebe64e35681f43f2',
    # batch.py
    '5a582c2814c3664f72d98ca865b4e51c2fed552e0bb8a587dca0423bbd83cccc':
        '6cbf7d5dcaa136f148e63dedb04f47618ab3ee4354e630e20b20dc36a1876997',
    # docker.py
    '8b94985fd55723af2f5f8832efcf165dec3933a25338a31f392e81db65bc6f3d':
        '8ffe7dd59a13a8f7e8ab203777dc031be8627a3b07b516737c333f1ff2bfe8c1',
    # test_data_runtime.py
    'f1afbc769df49b86d0f0b199678f5a9c8e522cef2efe5aa460a56b02a2ced855':
        None,
}


def frozen_tree_fingerprint(digest):
    return _FROZEN_TREE_EQUIVALENT.get(digest, digest)
