# Affected selection verification

The checks use disposable clones. Original experiments under the target
repositories are read-only inputs; generated sources are copied and imported,
and their original hashes are checked after comparison. No model calls run.
Python is 3.12. Both selection modes use one build, survive, and coverage worker.
These are single warm-cache runs on a shared developer machine, with some JSV
stages overlapping unit/Java/lifecycle checks, not isolated repeated benchmarks.

## Compilation fallback and json-schema-validator

The configuration is the exact slice in [batch baseline verification](batch-baseline-verification.md):
anchor `0c14a7a8664334fd10108895f84d8232f6c34019`, 20 first-parent commits,
15 developer, 23 Ouédraogo, and 131 KTester frozen methods. Image:
`survival-java:25` (`sha256:c5dfcc9eca90efdd5f4de755553f47c63e609ae85f3b8423d78b1b698455c8bd`).

The before-change batch run at `d2f5a1b` took **501.557 s** in survive. The new
all-mode run took **369.714 s**. Its comparison against the legacy reference
has **zero differences** across 3,549 method-state cells, 53,394 scope-line
cells, executable universes, and changed-scope coverage. The three developer
methods formerly blocked by `AbstractJsonSchemaTest` have their legacy states;
all 3,315 previously reported developer scope-line differences disappear.
See [fallback evidence](affected-selection-verification/compilation-fallback.json).

All versus affected also has **zero required differences**: frozen sources and
denominators, per-index/group/method cumulative states, first failures,
changed-line tables, coverage indices, and per-group scope-line output match.
Counts below exclude control at t, which executes all 169 methods in both modes.

| Group | All executed | Affected executed | Affected carried |
|---|---:|---:|---:|
| Developer | 103 | 70 | 33 |
| Ouédraogo | 161 | 81 | 80 |
| KTester | 1,123 | 960 | 163 |
| Total | 1,387 | 1,111 | 276 |

Affected avoids 19.90% of post-anchor executions but is slower on this slice.
The new classpath resolution, source/bytecode analysis, broader instrumentation,
and selected-source inventory have costs; fewer executions alone do not establish
a wall-time improvement.

| Stage, seconds | All | Affected |
|---|---:|---:|
| Prepare and scope | 81.391 | 106.959 |
| Import | 1.966 | 2.250 |
| Screen | 147.770 | 159.961 |
| Control | 58.867 | 49.726 |
| Survive | 369.714 | 434.765 |
| Cover | 1.598 | 1.402 |
| Report | 1.532 | 1.356 |
| Total wall time | 662.853 | 756.436 |

Peak test-JVM RSS is **466,026,496 bytes (444.44 MiB)** in all mode and
**440,651,776 bytes (420.24 MiB)** with the broadened affected instrumentation.
These are maxima of per-JVM Linux `VmHWM`, not aggregate concurrent process
memory. Peak heap-pool sums are 137,930,256 and 137,832,528 bytes respectively;
pool maxima need not occur simultaneously. Full evidence is in
[the JSV comparison](affected-selection-verification/json-schema-validator.json).

An additional per-method-line diagnostic found 42 differing method/line cells:
two generated `SpecVersionDetectorTest` methods at indices 5, 6, and 9 each have
seven additional initializer lines in affected evidence. They are the map
initializer and static block at lines 38 and 42–47 of `SpecVersionDetector.java`.
Selected execution changes where initialization probes are attributed; index 6
carries the index-5 observations. The group line union is identical, so none of
these attribution differences changes the required coverage tables. All cells
are listed in [method-line-differences.csv](affected-selection-verification/method-line-differences.csv).

The first affected attempt was stopped after discovering that Maven decorates
dependency-list paths with ANSI colors and `(optional)`. The parser correctly
treated the unrecognized paths as unknown and conservatively executed tests,
but this could not measure useful selection. Commit `9f190c7` strips those
decorations and adds a regression using the real format. The stopped attempt
is retained under `stopped-classpath-affected*`; reported numbers are from the
fresh successful attempt.

## Per-index performance target

The reference timings below are provisional: they overlapped DNS anchor
selection. They will be replaced by quiet-host measurements after the ongoing
baseline and policy runs finish.

The DNS check preserves the original configuration's flaky rerun and
`java.net.BindException` environment retry settings. An initial harness version
incorrectly disabled both, following the JSV comparison settings. That attempt
was stopped during anchor scope selection and retained at
`/tmp/affected-selection-dns`; it contributes no reported equality or timing
result. The corrected fresh run is at `/tmp/affected-selection-dns-exact`.

The 1.5× target applies to the two baseline checks, as clarified by the user;
regenerated policy snapshots do not use a reference at the original t.
For JSV, the anchor references execute all **169 frozen methods (303 invocations)**
in a single Surefire fork and pass. The all-agent reference takes **122.518 s** (3.849 s
compilation, 116.722 s in the frozen-test driver); the broader affected-agent
reference takes **128.705 s** (4.857 s compilation, 121.432 s in the driver).
One successful timed reference is measured for each distinct agent configuration.

The maximum full-execution index ratio, including control, is **0.480×** for
all mode and **0.386×** for affected mode. No JSV index exceeds 1.5×. These
single-run reference ratios do not imply that affected selection is faster
than all mode; the direct comparison above shows the opposite on this slice.

Every evaluated index has a four-way wall-time breakdown in
[all per-index timing](affected-selection-verification/jsv-all-per-index-performance.csv)
and [affected per-index timing](affected-selection-verification/jsv-affected-per-index-performance.csv).
Production build is the serial preparation span, including checkout and Maven
classpath/build work. Test compilation sums distinct snapshot compiler attempts,
deduplicating shared roots and including failed removal rounds. Test JVM time
sums the serial chunk processes, including their per-test coverage analysis.
Everything else is the non-build interval minus those two components: selection,
dependency analysis, helper/universe work, orchestration, and projection.
The intervals come from durable timestamped commit-completion logs; final
stage-wide aggregation and cleanup after the last commit are not assigned to
that commit. Control uses its complete recorded stage wall time. Workers are
one, so chunk/commit intervals do not overlap.
One-off denominator selection, generated-test import, and screening remain in
the stage totals; they are not repeated per-index survival costs.

See [ratio summary](affected-selection-verification/jsv-performance-summary.json)
and the exact [all](affected-selection-verification/jsv-reference-all.json) and
[affected](affected-selection-verification/jsv-reference-affected.json) references.
The reference command is `mvn -o -q test`, with production already built at t.
An untimed setup resolves Maven plugins/providers. The first offline attempt
exposed an uncached Surefire provider; that failed attempt is retained and is
not the reported successful reference.

Plain Maven cannot compile these generated snapshots into one shared test-class
directory: several contain different classes with the same binary name. The
reference therefore uses a small test-compile Mojo to compile each eligible root
inside Maven, and one Surefire wrapper to select the exact frozen methods in
isolated loaders, matching worker resource isolation. Test sources are not
rewritten. It uses the same test classpath, heap cap, JaCoCo version, include
patterns, and `output=none,dumponexit=false` settings. It does not run the survival
listener or compute per-method coverage; it verifies method identities and JUnit
outcomes. This adapter is the required accommodation for the colliding frozen
snapshots, rather than a stock target-project test-suite timing.

## Lifecycle, readers, and automated tests

All **858 tests passed**, including all 16 real Java integration fixtures,
in 213.83 s. The fixtures cover runtime reflection outside scope, test-helper
bytecode closure and source constants, nested classes, unknown mappings,
selection triggers and non-triggers, endpoint renames, segment/control history,
carried lines, invalid origins, iterative compilation beyond 100 errors,
concurrent resource-isolated chunks, and nonzero JVM memory evidence.
Two subsequent timing fixtures pass, checking shared-compilation
deduplication, component reconciliation, full-execution ratio eligibility, and
both measurements at a policy regeneration boundary.
A harness regression also verifies that DNS retains its original retry
settings. The current **861-test** suite passes CI: 845 Python tests on both
Python 3.12 and 3.14, and all 16 Java 25 fixtures. The Java workflow explicitly
includes the new affected-selection fixtures and rejects missing/skipped cases.

The [affected lifecycle fixture](affected-selection-verification/lifecycle.json)
uses real Docker workers, `survival stop`, status while running/stopped, and
resume. Resumed survival, first-failure, and changed-line tables are byte-identical
to the uninterrupted run. Developer reset/keep policies and completed policy
resume succeed without changing pools. A separate
[missing-decision repair](affected-selection-verification/policy-repair.json)
reconstructs an identical logical row in 8.355 s and leaves every pool unchanged.

Read-only `status` and `show report` succeed on the original finished DNS
experiment. Status reports all 179 builds, 178 survival commits, and 148 coverage
indices completed without rewriting historical hashes. Old pre-framework JSV
inputs have no `experiment.yaml`, as before; policy readers use the existing
baseline-config fallback for those inputs.

## Reproduction and revisions

```sh
.venv/bin/python scripts/affected_selection_compare.py setup --root /tmp/affected-selection-jsv
.venv/bin/python scripts/affected_selection_compare.py all --root /tmp/affected-selection-jsv --tool /path/to/pinned/tool
.venv/bin/python scripts/affected_selection_compare.py affected --root /tmp/affected-selection-jsv --tool /path/to/pinned/tool
.venv/bin/python scripts/affected_selection_compare.py compare --root /tmp/affected-selection-jsv
.venv/bin/python scripts/affected_selection_compare.py setup --project dnsjava --root /tmp/affected-selection-dns
.venv/bin/python scripts/affected_selection_compare.py all --root /tmp/affected-selection-dns --tool /path/to/pinned/tool
.venv/bin/python scripts/affected_selection_compare.py affected --root /tmp/affected-selection-dns --tool /path/to/pinned/tool
.venv/bin/python scripts/affected_selection_compare.py compare --root /tmp/affected-selection-dns
.venv/bin/python scripts/affected_selection_compare.py policy-all --root /tmp/affected-selection-jsv --tool /path/to/pinned/tool
.venv/bin/python scripts/affected_selection_compare.py policy-affected --root /tmp/affected-selection-jsv --tool /path/to/pinned/tool
.venv/bin/python scripts/affected_selection_compare.py policy-compare --root /tmp/affected-selection-jsv
.venv/bin/python scripts/affected_selection_performance.py reference --root /tmp/affected-selection-jsv --mode all
.venv/bin/python scripts/affected_selection_performance.py reference --root /tmp/affected-selection-jsv --mode affected
.venv/bin/python scripts/affected_selection_performance.py timing --root /tmp/affected-selection-jsv
.venv/bin/python scripts/affected_selection_performance.py policy-timing --root /tmp/affected-selection-jsv
.venv/bin/python scripts/batch_baseline_fixture.py --selection affected --root /tmp/affected-selection-lifecycle
SURVIVAL_JAVA_TEST_IMAGE=survival-java:25 SURVIVAL_JAVA_TEST_LIBS=/tmp/batch-baseline-java-libs SURVIVAL_REQUIRE_JAVA_TESTS=1 .venv/bin/pytest -q
```

Use fresh roots; setup does not overwrite an existing clone. Each mode has a
separate Maven volume initially copied from `docker_ktester-m2` mounted read-only,
except DNS affected shares the completed all run's Maven volume and validated
anchor method-coverage cache. That cache does not depend on survival selection;
its existing fingerprints are checked without rewriting them. This avoids
repeating the expensive coverage-scoped denominator selection. Its separate
survival executions and production checkouts remain fresh. Reference Maven
volumes are separate copies of the corresponding experiment cache.
The actual JSV all run pins `e03519e`; affected pins `9f190c7`. Subsequent changes
add historical-reader and policy-resume validation and verification helpers;
they do not change fresh measurement verdicts or scope-line projection. Exact
per-stage revisions, commands, logs, and timings remain in the scratch roots and
the checked-in evidence. Finished original experiments were not rerun or migrated.
