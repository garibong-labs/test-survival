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
HISTORICAL = (PRE_BOUNDED_COVERAGE, PRE_MULTI_RELEASE)


def survival_provenance_matches(previous, current):
    return previous == current or any(previous == {**current, **era} for era in HISTORICAL)


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
