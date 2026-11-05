# Batch baseline verification

## Reproduction and boundaries

The comparison uses a disposable clone at
`/tmp/batch-baseline-comparison/repo`. The original json-schema-validator
experiments are read-only inputs. No model calls are made: generated sources
are copied, explicitly imported, and screened, and the `generate` stage is
never invoked. The configured model credential is deliberately absent.

Run with the project's Python 3.12 environment and Docker image
`survival-java:25`:

```sh
.venv/bin/python scripts/batch_baseline_compare.py setup
.venv/bin/python scripts/batch_baseline_compare.py legacy --tool /path/to/pinned/tool
.venv/bin/python scripts/batch_baseline_compare.py batch --tool /path/to/pinned/tool
.venv/bin/python scripts/batch_baseline_compare.py compare
.venv/bin/python scripts/batch_baseline_compare.py policies
.venv/bin/python scripts/batch_baseline_fixture.py --root /tmp/batch-baseline-e2e
```

Both modes must use the same tool revision, Docker image, initial Maven cache,
configuration except execution mode, and generated inputs. `setup` refuses to
overwrite inputs/experiments. The runners enforce a one-hour limit per baseline
mode and use `survival stop` on expiry. These are single-run, warm-cache wall
times on a shared developer machine, not repeated performance benchmarks.

The actual baseline comparison pins tool revision
`e4b32ecbd0264df679324efaa82a33636120db6d` in
`/tmp/batch-baseline-tool`. Subsequent changes fix progress fingerprints and
completed-stage repair, and implement the separately tested policy/screening
features; they do not change the initial baseline worker's measured verdicts
or scope line projection. The pool and interruption fixtures use the subsequent
implementation. The image ID is
`sha256:c5dfcc9eca90efdd5f4de755553f47c63e609ae85f3b8423d78b1b698455c8bd`.
Independent Maven volumes `batch-baseline-legacy-m2` and
`batch-baseline-batch-m2` start as copies of `docker_ktester-m2` mounted read-only
for that copy. Workspaces and build products are not shared between modes.

An initial legacy startup exposed a `/tmp` versus `/private/tmp` Python source
mount mismatch. That attempt failed before measurement; its directory and log
are retained as `failed-startup-legacy*`. The harness now resolves the tool path
before passing `PYTHONPATH`. Reported timings are for the fresh successful runs.

## Comparison configuration

- Repository: json-schema-validator, anchor
  `0c14a7a8664334fd10108895f84d8232f6c34019` (1.1.0).
- Twenty first-parent commits, ending at
  `51fd82b420c3abde014f180ea109155de0d36480`; documentation-only commits carry
  previous state using `range.skip_untouched: main-sources`.
- One `dev` group with the `scope-targeted` denominator; Ouédraogo copied from
  `experiments/commit-survival/generated/ouedraogo`; KTester copied from
  `experiments/commit-survival-run2/generated/ktester`.
- Explicit scope: `PrefixItemsValidator`, `PropertiesValidator`, `TypeValidator`,
  `CollectorContext`, `SpecVersionDetector`, `JsonSchema`, `BaseJsonValidator`,
  `AnyOfValidator`, `OneOfValidator`, `JsonSchemaFactory`, all in
  `com.networknt.schema`.
- `screen.stable_runs: 1`, `dev.stable_runs: 1`, `survival.flaky_rerun: 0`,
  environment reruns disabled, one worker for build/survive/cover. Heap limits:
  launcher and JaCoCo 1 GiB, javac 512 MiB. Build/compile/test-class timeouts:
  600/300/120 seconds. Coverage uses alive-entering populations, at t and for
  changed lines. Exact generated method bodies are deduplicated; no matching
  downsampling.
- Frozen populations: 15 developer, 23 Ouédraogo, 131 KTester methods (169 total).
  The comparison asserts byte-identical frozen input sources and denominators.

The script is the complete executable configuration source; each generated
experiment also retains its YAML, resolved configuration, provenance and logs.
`source-hashes.json` records the original generated input hashes for a read-only
post-run check. `comparison.json` enumerates every method-state and scope-line
discrepancy rather than only aggregate counts. Legacy `error` and `timeout`
normalize to `fail`; `absent` and `compile-fail` remain distinct.

## Results

Both modes completed all 20 commits within one hour; no range reduction was
needed. Indices 1, 3, 7 and 17 were untouched and carried state. All 16 remaining
commits built successfully. Coverage consumers used 16 indices including t;
batch also measured the evaluable index not needed by a changed-line consumer.

| Stage | Legacy seconds | Batch seconds |
|---|---:|---:|
| Prepare and scope | 82.239 | 74.903 |
| Import | 2.042 | 1.970 |
| Screen | 144.157 | 143.710 |
| Control | 49.156 | 43.045 |
| Survive | 461.384 | 465.941 |
| Cover | 468.292 | 1.052 |
| Report | 1.474 | 1.093 |
| Total wall time | **1,208.763** | **731.765** |

That is **20m 08.763s versus 12m 11.765s**, a 39.46% reduction (1.65× overall).
The saving comes from eliminating coverage re-execution; the instrumented
survival pass itself was slightly slower. These timings include differing
later developer populations explained below, so they are not an isolated
measurement of batching overhead.

The comparison checked 3,549 cumulative method/index cells and 53,394 scope
line/index/group cells. Frozen populations and source bytes matched, all scope
executable universes matched, anchor coverage matched, and generated files in
the original finished experiments retained their recorded hashes. Batch emitted
no group coverage XML. Its `all_*` and `other_*` changed-line values are all blank.

### Every remaining survival difference

Three developer methods pass in legacy but first become `compile-fail` in batch
at index 10 (`7f1ec11b7a4b391577595e0d31d5825548c9a64f`):

| Class (under `com.networknt.schema`) | Method |
|---|---|
| `PropertiesValidatorTest` | `testDoesNotThrowWhenApplyingDefaultPropertiesToNonObjects` |
| `TypeValidatorTest` | `testNumeicValues` |
| `TypeValidatorTest` | `testNonNumeicValues` |

These losses carry through indices 10–20, including skipped index 17: **33
cumulative cells**, listed individually in
[survival-differences.csv](verification/batch-baseline/survival-differences.csv).
No generator survival differences remain after the requested state mapping.
Final survivors are legacy **3 / 0 / 49** and batch **0 / 0 / 49** for
developer / Ouédraogo / KTester.

The cause is the existing policy compilation semantics, not a different frozen
suite. After a whole-snapshot failure,
[`compile_snapshot`](../src/test_survival/adapters/snapshot_compile.py) explicitly
includes all non-test helper sources in its selected-file fallback, preserving
reflection-only helpers. Frozen `AbstractJsonSchemaTest` and
`AbstractJsonSchemaTestSuite` no longer compile: their references to
`URITranslator`, `SpecVersionDetector.detectOptionalVersion`/`detectVersion`,
and `JsonSchemaFactory.Builder.objectMapper`/`addUriTranslator` are incompatible
with index 10. Legacy's fallback in
[`run_file`](../src/test_survival/adapters/java_worker.py) compiles the selected
source and javac-inferred dependencies, avoiding these unrelated helpers.
Batch retains the requested policy path; changing that fallback to force parity
would also change policy snapshot semantics.

### Every remaining changed-line coverage difference

All differences are developer coverage caused by the compilation losses above.
Executable denominators are identical. Every other index/group value matches.

| Index | Executable scope lines | Legacy developer covered | Batch developer covered | Legacy percent | Batch percent |
|---:|---:|---:|---:|---:|---:|
| 10 | 287 | 89 | 0 | 31.010 | 0.000 |
| 13 | 220 | 31 | 0 | 14.091 | 0.000 |
| 15 | 10 | 8 | 0 | 80.000 | 0.000 |
| 18 | 33 | 12 | 0 | 36.364 | 0.000 |

Those four rows account for all eight differing count/percentage fields in
`changed-lines.csv`. Across the complete scope line CSVs, 3,315 developer
line/index cells change from covered to uncovered at indices 10–16 and 18–20.
Every coordinate and its cause are listed in
[scope-line-differences.csv](verification/batch-baseline/scope-line-differences.csv).
At index 10, legacy developer coverage records contain three passes and six
compilation failures; batch contains nine compilation failures. Later batch
developer populations are empty. This explains all 3,315 line differences;
there are no unexplained lifecycle, timeout, generator, or universe differences.

[comparison.json](verification/batch-baseline/comparison.json) contains the
machine-readable counts, timings, cause registry and exact changed-line fields.

## Developer regeneration and lifecycle fixtures

The standalone Java fixture passed on implementation revision `ab2bd3d`, using
`scripts/batch_baseline_fixture.py` and a fresh
`/tmp/batch-baseline-e2e/target`. Its source history has an untouched commit, a
production change with an updated developer test, and a later equivalent
production change. It includes frozen resources and a reflection-only helper.

- The batch baseline measured one developer parent, a derived coverage-scoped
  child, and imported generated tests without model calls.
- Full developer suite coverage selected four methods into a regeneration pool
  at index 2. The funnel records `stable_runs: 1`, `stable_passed: 4`, `kept: 4`.
- `release-reset` and `half-reset` opened a second segment and executed at
  indices 2 and 3. `half-fill` with `developer: keep` retained its baseline
  segment and coverage reference. Global regeneration was false.
- `survival status` observed a live batch; `survival stop` terminated it. A
  subsequent resume completed, and `survival.csv`, `first-failures.csv` and
  `changed-lines.csv` matched the uninterrupted run byte for byte. Status
  retained the stopped event and showed completed survival after resume.
- Resuming the completed policy run preserved every pool file byte for byte.
  Baseline output had no coverage XML and blank all/other fields.

See [java-fixture.json](verification/batch-baseline/java-fixture.json) for the
funnel, checks and timings. This fixture is separate from the timed comparison;
it covers segment execution, while the real-project command below is restricted
to pool construction as requested.

The real-project exercise also completed. The harness created a separate
`experiments/policies` directory, read the batch comparison baseline, and ran
`survival policy plan` followed by `survival policy run --until pools`, restricted
to `policies.groups: [dev]`. Global developer regeneration was false, with the
three requested per-policy settings. A scratch-only release tag points to index
2; the baseline threshold schedules index 10. There were no generator pools or
model calls. Plan took 0.182 seconds; both pools took 823.753 seconds (13m 43.753s).

| Point | Discovered methods | Selected | Stable runs | Stable passes | Kept |
|---|---:|---:|---:|---:|---:|
| Release, index 2 | 219 | 22 | 1 | 15 | 15 |
| Threshold, index 10 | 290 | 22 | 1 | 18 | 18 |

Both points measured the full current developer suite before denominator
selection and screening. The real-project run stopped at pools as requested;
the smaller fixture above verifies opening and executing the new segments.
[policy-pools.json](verification/batch-baseline/policy-pools.json) preserves the
exact plan, funnel counts, durations and implementation revision.

## Automated checks and limits

The complete suite passed: **792 tests, zero skips, 175.82 seconds**, with Python
3.12.12 and the pinned fixture libraries. All **14 real Java fixtures** passed;
the CI fixture-report guard also accepted the extracted Java results. The
scope-universe fixture is included in the required Java CI workflow. See
[tests.json](verification/batch-baseline/tests.json) for the exact command.
GitHub's Python and Java workflows both passed on `bdfff28`, which contains all
implementation, tests and comparison evidence; the final follow-up adds only
this completed pool report.

Coverage includes historical legacy fingerprints/layouts, scope-only batch
projection, derived groups, skipped aliases, mocked build failures, dead-method
omission, bounded parallel dispatch, missing-coverage repair, projection repair,
per-policy developer fallbacks/targets, and configurable stability runs. A real
fixture covers developer pool creation through segment execution and actual
stop/resume; real-project pools exercise the larger developer suite.

Literal cross-mode equality was not achieved for the documented developer
helper-compilation cases. The requested policy execution path is preserved;
legacy mode remains unchanged. No unexplained comparison differences remain.
The timed runs had no build failures or JVM timeout/fallback events, so those
branches are covered by fixtures rather than claimed as observed in this slice.
No finished experiment was edited, no model calls were made, and no archive or
pruning command was run against a real experiment.

## Review follow-up

Disposable regression fixtures reproduced the three review findings: local
children remeasuring an imported parent's methods, a saved candidate trigger
without its required pool entry, and non-executing coverage repairs being
accepted as complete. The fixes derive children in dependency order (including
import-only runs without Java), validate required pool entries before execution,
and use the same coverage-usability rule for caching, repair and projection.
Coverage repair retains the original survival verdict. Additional cases cover
chained children, untouched imported files, and empty local populations.
The complete suite passed again: **798 tests, zero skips**, including all 14
required Java fixtures (138.10 seconds).

A fresh real Java fixture at `/tmp/batch-baseline-review-e2e` passed developer
pool/segment execution and stop/status/resume with byte-identical resumed
survival, first-failure and changed-line outputs. The timed comparison above
remains the original measurement; no finished experiments were rerun or edited
for these fixes.

The second review reproduced two further issues in disposable fixtures: batch
developer imports accepted a different resolved coverage scope, and progress
reported all commits complete while a batch still needed coverage repair.
Import compatibility now records the selected class/source-file pairs, checks
them before copying and reuse, and rejects a changed scope selection before
retaining imported denominators. Legacy import identity remains unchanged.
Batch imports now require the destination to run through `--until scope`.

Resume, log progress, and panel progress share the batch completeness audit.
It checks measurement identity, usable per-test lines and group projections,
and carries valid failure verdicts forward even while their coverage needs
repair. Twelve new regression cases cover scope mismatches and missing scope
proof, reselection, read-only progress, stale identity, missing projections,
missing lines and non-executing repairs, including control at t.

The full suite passed with **810 tests, zero skips**, including all 14 required
Java fixtures (169.34 seconds). A fresh standalone Java fixture at
`/tmp/batch-baseline-second-review-e2e` also passed developer pool/segment
execution and actual stop/status/resume, with byte-identical resumed survival,
first-failure and changed-line outputs. These checks used disposable fixtures;
the original timed comparison and finished experiments were not rerun or edited.

The third review reproduced two inspection defects: status crashed before
screening produced frozen denominators, and completeness audits rewrote the
exclusion summary. Audits now wait for their prerequisites, and exclusion
calculation is separate from publication by measuring stages. Twelve regression
cases cover fresh/prepared/scoped status, missing audit inputs, and completed
audits with or without an existing summary and with writes denied. The read-only
checks compare inode, modification/change timestamps and contents, detecting
rewrites even when the bytes would remain identical.

The full suite passed: **822 tests, zero skips**, including all 14 required Java
fixtures (149.84 seconds). The checks use disposable fixtures; no finished
experiments or original comparison measurements were rerun or modified.
