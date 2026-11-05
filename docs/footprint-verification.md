# Footprint implementation and verification

The Part 1 review was accepted on 2026-11-05. At the user's subsequent request,
`docs/footprint-review.md` was removed from the published branch. This document
records the implementation decisions and verification evidence instead.

## Decisions

- Prepare retains every successful index build. Survival releases non-anchor
  indices outside the planned coverage set only after aggregation validates all
  measured, imported and derived groups. Coverage retains its indices (including
  predecessors resolved across skipped commits) until all groups and coordinator
  line-universe validation succeed. Policy windows release non-boundary indices
  after their last batch; regeneration boundaries remain through both window and
  regeneration consumers. Index zero remains until successful whole-run
  completion; a pool releases its anchor after publication and source, resource
  and execution-hash verification. Failed/in-flight work is retained.
- Each index is built once in an uninterrupted run. Peak disk is not a constraint;
  cleanup prevents accumulation after completion. Verification counts every
  restoration attempt, including interrupted attempts and nested pools; the
  uninterrupted run must have zero. Restoration is reserved for resume, legacy
  workspaces and genuine consumers after release.
- Later consumers restore a released workspace via checkout/build. Original
  build records/logs remain authoritative. Restoration writes separate
  diagnostics and stops on SHA, dependency-content identity or verdict mismatch.
  Old experiments without identity records remain readable and are not pruned;
  restoration of a missing legacy workspace without an identity fails closed.
- `--keep-work` disables release/cleanup in baseline and policy runs, including
  background runs and pools. Pool-local compile caches, generation/policy
  histories and KTester completion records remain in their existing locations.
  Linux host permission failures retry the same guarded deletion in the configured
  container, retaining command diagnostics without changing tree permissions.
- Small failed file subtrees and shared failed policy batches remain whole.
  Successful scratch is deleted after its owning stage succeeds. Retry evidence
  is copied to independent files before a worker clears its original scratch;
  sibling job input/log files are preserved too. This avoids stale directory
  aliases across Docker Desktop host/guest mounts.
- Compile history uses per-context gzip-member appends, persistent flock locks,
  linked start/finish/interrupted IDs, and a 16 KiB successful-log limit. Original
  byte count, SHA-256 and truncation flag accompany text; errors/timeouts keep
  full text. Both event layouts remain readable.
- All coverage XML, including dev-scope XML, is retained and compressed. Bulk
  raw/coverage/per-method text uses gzip; small summaries and state stay plain.
  Logical result hashes cover uncompressed bytes. Generated artifacts are never
  cleanup targets. SHA-shared checkouts remain deferred.
- Verification uses the accepted synthetic Java/Ouedraogo-shaped import with no
  model calls, one policy pool shared by reset/fill, and separate sequential
  baseline/policy runs. The main reference completed well under one hour.

## Fixture and reproduction

The reference is main `01ae5d006bd4e900fe3e708eee28895fed9d4276`, after archive
PR #30 merged. Python is 3.12.12. All runs use the same existing Docker image
`survival-java:25`, immutable image ID
`sha256:c5dfcc9eca90efdd5f4de755553f47c63e609ae85f3b8423d78b1b698455c8bd`.
The image uses Python 3.14 inside its Java workers. Each run starts with a fresh
copy of the same warmed Maven volume (`footprint-e2e-seed`).

`scripts/footprint_fixture.py` contains the complete baseline and policy YAML and
creates `/tmp/footprint-e2e/target`, refusing to overwrite an existing target.
It creates four fixed-date commits: anchor, documentation-only skip, changed
return value plus updated developer test, and equivalent production branch.
The scope is one class/three focal methods. Four Dev methods exercise two source
files, a reflection-only helper and a resource; two imported synthetic methods
form the Ouedraogo group. Both project and coverage-scoped Dev groups are used.
Three stability runs and changed-line coverage run through real Docker/JDK/Maven.
A release boundary at index 2 creates one developer pool used by reset and fill.

The execution sequence is:

```sh
.venv/bin/python scripts/footprint_fixture.py
git worktree add --detach /private/tmp/footprint-main 01ae5d006bd4e900fe3e708eee28895fed9d4276
PYTHONPATH=/private/tmp/footprint-main/src .venv/bin/python -m test_survival.cli run /tmp/footprint-e2e/target/experiments/baseline --until scope --no-panel
docker run --rm -v footprint-e2e-m2:/from:ro -v footprint-e2e-seed:/to survival-java:25 sh -c 'cp -a /from/. /to/'
.venv/bin/python scripts/footprint_run.py /private/tmp/footprint-main main
.venv/bin/python scripts/footprint_run.py . final-uninterrupted
.venv/bin/python scripts/footprint_run.py . final-resumed --interrupt
.venv/bin/python scripts/footprint_compare.py main final-uninterrupted /private/tmp/footprint-main .
.venv/bin/python scripts/footprint_compare.py main final-resumed /private/tmp/footprint-main .
.venv/bin/python scripts/footprint_compare.py final-uninterrupted final-resumed . .
.venv/bin/python scripts/footprint_metrics.py main final-uninterrupted final-resumed
```

The harness retains prior experiments and snapshots completed runs outside the
active target. It samples checkout counts/bytes every 150 ms, saves command logs,
stage state and wall-clock timing, and aborts after one hour. The interruption
uses `survival stop` after a real policy worker writes `batches.tsv`; it resumes
with the same command. Before resuming it snapshots interrupted work and hashes
all existing original build and pool artifacts. Afterward it checks those hashes
and the copied failure evidence. None of the measurement-only ta4j,
json-schema-validator or dnsjava experiment folders was modified.

## Comparison contract

The non-negotiable comparison is exact survival states, first failures, requested
and frozen methods, coverage line universes/hits, changed-line/exposure totals,
policy pools/selections/segments/curves, and every report's measured numbers.

All physical files are inventoried with byte counts and SHA-256; bulk `.gz` paths
are mapped to their logical names and decompressed. Missing/extra scientific
paths fail. JSON field and array order is retained in normalized serialization;
CSV rows are neither reordered nor rounded. Reports, sources, SVG and PNG are
compared byte-for-byte after only the applicable substitutions below.

| Field or artifact | Accepted handling |
|---|---|
| `started`, `finished`, `imported`, `timestamp`, `checked_at`, `archived_utc` | Execution timestamps only; commit dates remain exact. |
| `seconds`, `elapsed_seconds`, `phase_seconds`, `latency` | Durations; also the per-method coverage CSV seconds column and JUnit report `time`/text duration. |
| Known tool roots, command/cwd paths | Normalize tool mount roots. Figure renderer cwd is `/experiment` rather than the removed checkout; no rendered content exemption. |
| `runner_sha256` | Verify every digest against the actual revision's source bytes before normalizing the map. |
| Generated validation `input_sha256` | Recompute its source/repetition/runner-provenance key before normalization. Other scientific fingerprints remain exact. |
| UUIDs, lifecycle PIDs/container/run IDs, Surefire temporary process paths | Canonicalize operational identities; compiler references must resolve to linked starts/finishes. |
| JaCoCo `sessioninfo.id`, `start`, `dump` | Normalize JVM session identity/timestamps; all class IDs, counters and lines remain exact. |
| Compile cache hit/miss/key/origin/timing | Check referenced events separately; compare command/context/verdict and original output hashes across layouts. |
| Cleanup/restoration/cache/scratch/run lifecycle history | Inventory and validate separately. Extra restore/retry events and intentionally removed successful scratch are expected. Original measured build results still compare. |
| Volatile-content digests such as KTester `final_summary_sha256` | Accepted in principle only after validating original content; absent in this fixture, so no blanket hash exemption is implemented. |

Original command logs are checked separately, canonicalizing only their UUID
path components. Compile start/finish cardinality, reference integrity, output
hashes and truncation bounds are checked. Additional workspace identity,
restoration and cleanup files are listed separately from scientific outputs.

## Coverage of documented output families

| Family in result-formats / regeneration-policies | Evidence |
|---|---|
| Anchor, commits, builds, skip metadata, baseline JUnit reports | Real fixture; original build records and command logs compare. |
| Frozen Dev sources/resources, inventories, scope, exclusions, method coverage | Real fixture, plain main versus gzip branch; source/resource hashes stay physical. |
| Generated sources/import/funnel/validation and frozen denominators | Synthetic import, real compilation/screening, exact source bytes; no live model call. |
| Raw, derived raw, raw coverage, XML (including dev-scope), line CSV | Every fixture index/group, transparent decompression and strict scientific comparison. |
| Survival/first failure/alive/changed lines/coverage consistency | Exact comparison, including skipped index and predecessor alias. |
| Tables, README report section, SVG, PNG, final summary | Exact rendered content and normalized metadata comparison. |
| Policy plans, pool sources/frozen records, batches/runs, selections, segments, curves, coverage, reports | One shared developer pool; reset and fill; resumed pool/build records are hash checked. |
| Compile events, caches, workspace identities/restoration, stage/run state | Separate operational inventory and lifecycle/provenance checks; concurrent writer and crash-tail unit tests. |
| KTester datasets/completion, live native responses/provider prompts, generation/policy force histories | Not end-to-end exercised; preservation/configuration/completion and existing generator tests cover these paths. No live model calls. |
| Build failures, mismatching restorations, quarantine, missing/failed coverage, concurrent workers | Dedicated failure/parallel fixtures and unit tests; the end-to-end target has successful builds and intentional test failures. |
| Archive ZIP/manifests and historical dashboard layouts | Existing archive/dashboard test suites; no archive run against a real experiment. |

An initial interrupted verification attempt exposed a Docker Desktop stale
host-rename alias during worker cleanup. It failed, was retained for diagnosis,
and led to independent copy-based retry preservation. Linux CI also exposed root-owned gzip files that host users could not read;
worker JSON/CSV permissions now match the prior readable layout and compressed
XML retains its original permissions. A real container fixture verifies guarded
cleanup of container-owned work. Final measurements below
use the corrected implementation; earlier diagnostic runs are not substituted
for the final interrupted/resumed verification.

## Measurements

All final scientific comparisons passed: **232 comparable artifacts**, zero missing
or differing paths, identical compile-event semantics and original command logs.
This includes the uncompressed dev-scope XML and exact PNG bytes. Baseline final
survivors are Dev 3/4, Dev-scope 3/4 and synthetic Ouedraogo 1/2. Both reset and
fill policy outputs are identical across the three runs. The final branch runs
used runtime commit `5ca8763`; subsequent changes are verification scripts,
tests and documentation only.

Counts are regular files and logical bytes (`stat().st_size`), including hidden
files, locks, preserved caches and failed scratch. Directories are not counted.
These are small-fixture measurements, **not estimates for the real experiments**.

| Run | Files | Bytes | Combined wall-clock seconds |
|---|---:|---:|---:|
| main | 1,157 | 55,880,704 | 94.628 |
| final-uninterrupted | 549 | 775,716 | 116.875 |
| final-resumed | 583 | 820,551 | 103.991 |

Uninterrupted reduction: **52.55% fewer files** and **98.61% fewer bytes**.

| Run | Successful scratch removed (files / bytes) | Failed scratch retained (files / bytes) | Protected compile cache (files / bytes) |
|---|---:|---:|---:|
| main | not instrumented | not instrumented | 36 / 14,977 |
| final-uninterrupted | 521 / 54,789,678 | 52 / 204,007 | 56 / 22,967 |
| final-resumed | 521 / 54,789,665 | 83 / 247,854 | 56 / 22,967 |

Scratch-removal totals are cumulative successful deletions and exclude released
checkout trees. Failure totals are measured from retained paths after completion,
including retry snapshots/job logs and shared runtime evidence. The resumed run
keeps 28 interrupted work files plus its saved job input/log and snapshot record;
38 pre-existing original build/pool artifacts also pass the resume hash check.
No checkout directories remain after either final branch run. Compile caches
remain, including the pool-local cache; rebuilding can increase cache entries
because existing cache identity includes file metadata.

| Output family (including pool) | Main files / bytes | Uninterrupted files / bytes | Resumed files / bytes |
|---|---:|---:|---:|
| checkouts | 92 / 32,837 | 0 / 0 | 0 / 0 |
| compile-events | 105 / 124,762 | 8 / 49,888 | 8 / 49,898 |
| compressed text | 48 / 340,346 | 48 / 61,283 | 48 / 61,286 |

### Sampled checkout peaks

Sampling every 150 ms can miss shorter-lived peaks. Counts include each numeric
checkout tree and its files, across the outer experiment and nested pools; marker
files are excluded from peak tree bytes. The combined maximum includes finished
baseline trees still resident while policy runs on main. Each column's maximum
is taken independently.

| Stage / scope | Main trees / bytes | Uninterrupted trees / bytes | Resumed trees / bytes |
|---|---:|---:|---:|
| combined | 7 / 32,284 | 2 / 18,222 | 2 / 18,222 |
| baseline/prepare | 3 / 21,041 | 2 / 18,222 | 2 / 18,222 |
| baseline/scope | 3 / 21,041 | 1 / 15,403 | 1 / 15,403 |
| baseline/screen | 3 / 21,041 | 1 / 15,403 | 1 / 15,403 |
| baseline/control | 3 / 21,041 | 1 / 15,403 | 1 / 15,403 |
| baseline/survive | 3 / 21,041 | 2 / 18,222 | 2 / 18,222 |
| baseline/cover | 3 / 21,041 | 2 / 18,222 | 2 / 18,222 |
| baseline/report | 3 / 21,041 | 1 / 15,403 | 1 / 15,403 |
| policy/policy-pools | 1 / 2,818 | 1 / 2,818 | 1 / 2,818 |
| policy/policy-survive | 4 / 11,243 | 2 / 5,607 | 2 / 5,607 |

### Wall-clock seconds per stage

Baseline values come from `final-summary.json.phase_seconds`; report and policy
values come from stage `started`/`finished` timestamps, checked against run logs.
The resumed policy-survive value adds the interrupted attempt (8.411 s) and the
successful retry. The retained logs show both starts and the stop. Generate is
an imported completed stage (0.000 s); import/launch/stop overhead is included
in the combined command wall clock above, not silently assigned to another stage.
The pool row is nested within policy-pools and must not be added twice.
These single runs establish correctness and show overhead; they are not a
controlled performance benchmark.

| Stage | Main | Uninterrupted | Interrupted/resumed |
|---|---:|---:|---:|
| baseline/prepare | 12.773 | 15.037 | 13.793 |
| baseline/scope | 8.865 | 9.495 | 9.239 |
| baseline/generate | 0.000 | 0.000 | 0.000 |
| baseline/screen | 2.503 | 2.640 | 2.516 |
| baseline/control | 6.300 | 6.584 | 5.620 |
| baseline/survive | 13.568 | 19.870 | 15.530 |
| baseline/cover | 26.595 | 37.146 | 29.578 |
| baseline/report | 0.715 | 0.981 | 0.653 |
| policy/policy-pools | 10.750 | 11.642 | 11.608 |
| policy/policy-pool-dev-scope-2 | 10.749 | 11.624 | 11.589 |
| policy/policy-survive | 11.715 | 12.633 | 14.470 |
| policy/policy-cover | 0.004 | 0.003 | 0.004 |
| policy/policy-report | 0.003 | 0.003 | 0.003 |

### Validation and limits

The main reference ran in 94.628 seconds, so no fixture reduction was required.
All three pairwise comparisons pass using the committed comparator. Original
command logs remain identical after UUID filename normalization, and every
restoration's SHA/verdict/dependency contents match its authoritative build.
The resumed-only `state/stop.lock` is an expected persistent lifecycle lock and
is inventoried separately. Status/show, dashboard and policy baseline readers
also loaded the plain main experiment with the new code; unit fixtures exercise
plain and gzip imports/reporting, corrupt/ambiguous files and uncompressed hashes.

The fixture does not run a live model or KTester generation. It does not predict
real-project disk savings or wall-clock overhead: its checkout trees are tiny,
and most byte savings here come from successful scratch/shared library removal.
Real experiment folders remain measurement-only. SHA-sharing is deliberately
not implemented. Failed trees and protected histories/caches can keep an
experiment's work directory nonempty even after successful completion.

Full local evidence is under `/private/tmp/footprint-e2e/`: the three named run
folders, comparison JSON/diffs, `metrics.json`, and the interrupted-work/hash
snapshots. The scripts make these checks repeatable without touching production
experiments. The removed Part 1 document is not republished.

Final runtime CI passed: **732 fast tests on Python 3.12 and Python 3.14**, plus
**11 real Java integration fixtures**, with zero skips/failures in the Java
report. The [Python workflow](https://github.com/garibong-labs/test-survival/actions/runs/41982284215)
and [Java workflow](https://github.com/garibong-labs/test-survival/actions/runs/41982284637)
verify runtime commit `5ca8763`. The 20 notification-security tests, Python
compilation, and dependency compatibility checks also passed. Local focused
coverage includes historical main fingerprints/progress, output permissions,
container-owned cleanup, and immutable pool verification.

```sh
SURVIVAL_JAVA_TEST_IMAGE=survival-java:25 \
SURVIVAL_JAVA_TEST_LIBS=/private/tmp/footprint-e2e/main/experiments/baseline/work/libs \
SURVIVAL_REQUIRE_JAVA_TESTS=1 .venv/bin/pytest -q --strict-markers
```
