"""Compatibility for measured survival records preceding execution-only changes."""

# PR #21 (0871fbb): these three modules change only launch orchestration and
# dead-method bookkeeping for phase A. All other provenance must still match.
PRE_BOUNDED_COVERAGE = {
    'java_maven.py': '56444d206dc46558a90e59e88ed948589b15ceccb85e240a808620e59a1844e1',
    'java_worker.py': '28f110f37c458d7b371a1e8ec139a4aacd088c57f56c8b4209d5e0560727cec5',
    'policy_coverage.py': '55641f372e3d7840830563d58b3d01373d809621a3ea4fb1ec948d329e3e3d2b',
}


def survival_provenance_matches(previous, current):
    return previous == current or previous == {**current, **PRE_BOUNDED_COVERAGE}
