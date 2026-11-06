# Affected selection verification

All experiments run in disposable scratch clones. Original target experiments
are read-only inputs; imported generated sources are hash-checked after the
comparisons. No model calls run. Python is 3.12. Both modes use **one** build,
survive, and coverage worker. Final survival stages and Maven references run
serially without another local benchmark. These are single warm-cache timings,
not repeated statistical benchmarks. Earlier preparation stages reused below
are identified separately.

## json-schema-validator and compilation fallback

This is the exact [batch comparison slice](batch-baseline-verification.md):
anchor `0c14a7a8664334fd10108895f84d8232f6c34019`, 20 first-parent commits,
Java 25 (`survival-java:25`, image
`sha256:c5dfcc9eca90efdd5f4de755553f47c63e609ae85f3b8423d78b1b698455c8bd`).
Control executes all 169 frozen methods in both modes. Post-anchor counts are:

| Group | All executed | Affected executed | Affected carried |
|---|---:|---:|---:|
| Developer | 103 | 70 | 33 |
| Ouédraogo | 161 | 81 | 80 |
| KTester | 1,123 | 960 | 163 |
| Total | 1,387 | 1,111 | 276 |

All required comparisons have **zero differences**: per-index/group/method states
and first failures, changed-line coverage, group scope-line output, and frozen
sources. Affected carries **19.90%** of evaluations and reduces survival wall time
**6.96%**, from **286.205 s** to **266.283 s**. Peak test-JVM RSS is **396.68 MiB**
for all and **419.17 MiB** for affected. See [complete JSV evidence](affected-selection-verification/json-schema-validator.json).

Against the legacy reference, the new all run has **zero differences** across
3,549 method-state cells and 53,394 scope-line cells, scope universes, and
changed-scope coverage. The three formerly contaminated developer methods regain
their legacy states: `PropertiesValidatorTest.testDoesNotThrowWhenApplyingDefaultPropertiesToNonObjects`,
`TypeValidatorTest.testNumeicValues`, and `TypeValidatorTest.testNonNumeicValues`.
All 3,315 old developer scope-line differences disappear. See
[fallback evidence](affected-selection-verification/compilation-fallback.json).
Survive-stage time before the fallback change was **501.557 s** at `d2f5a1b`;
the first fallback implementation took **369.714 s**, and the final optimized run
above takes **286.205 s**. The last improvement also includes coverage-analysis
optimizations, so it is not a fallback-only causal timing estimate.

An additional diagnostic finds 42 differing per-method-line cells: two generated
`SpecVersionDetectorTest` methods at indices 5, 6, and 9 have seven additional
initializer lines (38 and 42–47) in affected evidence. Selected execution changes
which method initializes the class; index 6 carries index 5's attribution.
Group line unions remain identical, so required coverage outputs do not differ.
Every cell is in [the JSV attribution CSV](affected-selection-verification/method-line-differences.csv).

The quiet-host references run 169 frozen methods / 303 invocations across 32
snapshots in one Surefire JVM: **21.284 s** with all-agent settings and **24.153 s**
with affected-agent settings. Every post-anchor full-execution index is below
1.5×. Including control, the original run maxima are **1.647×** and **1.622×**;
median ratios are **0.642×** and **0.662×**.

Profiling found that the idle image-runner container ignored termination as PID
1 and consumed Docker's five-second stop grace period. Adding `--init` reduced
a direct teardown measurement from **5.166 s** to **0.101 s**; Compose retains its
service-owned init setting. See [teardown profile](affected-selection-verification/container-stop-profile.json).
Fresh control-only rechecks at `cfa6170` reproduce all states, scope lines,
dependency records, and the scope universe exactly. Control wall times are
**33.190 s / 1.559×** for all and **35.626 s / 1.475×** for affected.
The original full-run timings are retained, not overwritten by these rechecks.
See [control rechecks](affected-selection-verification/jsv-control-recheck.json).

**Remaining JSV exception:** all control is 1.26 s above the strict 1.5× threshold.
Its four existing JVM chunks take 23.669 s versus the reference's single-JVM
18.460 s; compilation takes 2.386 s, and other work takes 7.135 s. That residual
includes helper preparation, scope-universe construction, container/Python
orchestration, and result projection. Further reduction would require changing
chunk boundaries or retaining worker/helper processes across jobs; those lifecycle
changes are not included in this PR. This is reported as an exception, not a
claim that every measured index meets the target.

## dnsjava, indices 0–60

The scratch clone preserves the original `experiments/commit-survival`
configuration, including flaky/environment retry settings used during anchor
selection. Batch measurement retains the baseline's documented behavior of
ignoring those reruns. The anchor is
`4c51bf24b320142edd2d3fe6932e767a22575bc4`, using Java 17, with 353 developer-scope
and 75 imported Ouédraogo methods. Control executes all 428 in both modes.
There are 47 evaluated post-anchor indices, ten skipped-untouched positions, and
three production-build failures (23, 44, 45). Both runs consequently report the
same six unavailable group-coverage records; they are not equality differences.

All required comparisons have **zero differences**: method states and first
failures, changed-line coverage, group scope-line output, and frozen sources.

| Group | All executed | Affected executed | Affected carried |
|---|---:|---:|---:|
| Developer scope | 6,583 | 3,516 | 3,067 |
| Ouédraogo | 3,491 | 1,058 | 2,433 |
| Total | 10,074 | 4,574 | 5,500 |

Affected carries **54.60%** of post-anchor evaluations and reduces survival wall
time **40.73%**, from **2,316.236 s** to **1,372.799 s**. Peak test-JVM RSS is
**478.01 MiB** for all and **500.08 MiB** with broadened affected instrumentation.
See [complete DNS evidence](affected-selection-verification/dnsjava.json).

The additional per-method-line diagnostic has 27 differing cells, all in
`Message.java`: the static `emptyRecordArray` initializer at line 59 (later 60)
and Lombok's `@Slf4j` logger at line 26. Selection changes which method first
initializes the class; carried rows preserve their prior attribution. The group
line unions remain identical. Every cell is in
[the DNS attribution CSV](affected-selection-verification/dns-method-line-differences.csv).

The quiet-host Maven references run 428 frozen methods / 478 invocations across
41 isolated snapshots in one Surefire JVM: **44.839 s** for all-agent settings
and **45.346 s** for affected-agent settings. The maximum affected full-execution
ratio, including control, is **1.402×**; none exceeds 1.5×. All mode's median is
**0.952×**, and its maximum excluding index 5 is **1.312×**.

**Index 5 is the one performance-target exception:** all mode takes 311.282 s
excluding production build, **6.942×** the anchor reference. Its test JVMs take
299.46 s. Production commit `85ff1c91` changes dispatch to
`sendAsync(Message, Executor)`, bypassing the frozen fixture's one-argument
override and its canned responses. Tests then wait on real DNS requests.
The upstream commit fixes its own tests, but applying that fix would change the
required frozen sources. Changing network/timeout behavior would also change the
experiment. This wait is therefore retained and reported, with
[thread-stack and source evidence](affected-selection-verification/dns-index-5-network-wait.json).
Affected index 5 also encounters the wait; it carries 158 methods and is not a
full-execution index subject to the target.

## Stage wall times and per-index accounting

| Stage, seconds | JSV all | JSV affected | DNS all | DNS affected |
|---|---:|---:|---:|---:|
| prepare-scope | 184.598 | 111.274 | 4,487.144 | 488.775 |
| import | 3.271 | 1.973 | 0.235 | 0.272 |
| screen | 289.537 | 134.611 | 47.468 | 43.549 |
| control | 35.059 | 39.176 | 58.820 | 63.574 |
| survive | 286.205 | 266.283 | 2,316.236 | 1,372.799 |
| cover | 1.669 | 1.581 | 4.399 | 4.699 |
| report | 1.660 | 1.416 | 2.391 | 2.292 |
| Total recorded stage wall time | 801.999 | 556.314 | 6,916.693 | 1,975.960 |

The totals are not an end-to-end speedup comparison: DNS affected reuses the
validated anchor method-coverage cache (18.881 s scope loading versus 3,940.648 s
original selection), and JSV all preparation/screening overlapped earlier work.
Production builds and final survival measurements remain separate executions.
Only the stated final survival-stage comparisons use quiet-host measurements in
both modes. One-off denominator selection, imports, screening, and policy pools
are reported separately from per-index survival cost.

Every evaluated baseline index, including control, has production build, test
compilation, test JVM, other, total, executed/carried counts, and eligible ratio:

- JSV [all](affected-selection-verification/jsv-all-per-index-performance.csv) / [affected](affected-selection-verification/jsv-affected-per-index-performance.csv).
- DNS [all](affected-selection-verification/dns-all-per-index-performance.csv) / [affected](affected-selection-verification/dns-affected-per-index-performance.csv).
- Ratio summaries: [JSV](affected-selection-verification/jsv-performance-summary.json) / [DNS](affected-selection-verification/dns-performance-summary.json).
- Per-index/group executed and carried counts for both baselines:
  [294 count rows](affected-selection-verification/baseline-selection-counts.csv),
  reconciled with each final summary.

Production build uses serial prepare-event spans, including checkout and Maven
classpath/build work. Test compilation sums distinct compiler attempts, including
failed removal rounds, deduplicated by log. Test JVM time sums serial chunks and
includes per-method survival/scope analysis. Other is the non-build interval
minus compilation and JVM time. Commit intervals use durable completion-event
timestamps; a progress refresh after an event is charged to the following span.
Final aggregation/cleanup after the last commit is retained in stage totals,
not assigned to that index. Control uses the full CLI stage wall time, including
container teardown. Workers are one, so intervals do not overlap.

Peak RSS is each test JVM's Linux `VmHWM`, not aggregate concurrent process memory.
The evidence JSONs also retain peak heap-pool sums and nonheap-pool sums; those
pool maxima need not be simultaneous.

## Maven reference and optimizations

The 1.5× target applies only to baseline checks, as the user clarified. One
successful timed `mvn -o -q test` reference per distinct agent configuration runs
at t with production already built. Plugin/provider setup is untimed. Reference
records include exact agent settings, wrapper hashes, method/invocation counts,
and a single Surefire PID:
[JSV all](affected-selection-verification/jsv-reference-all.json),
[JSV affected](affected-selection-verification/jsv-reference-affected.json),
[DNS all](affected-selection-verification/dns-reference-all.json),
[DNS affected](affected-selection-verification/dns-reference-affected.json).

Generated snapshots contain colliding binary class names, so stock Maven cannot
compile them into one shared test directory. A small test-compile Mojo compiles
the frozen roots inside Maven; one Surefire wrapper runs the exact methods with
isolated snapshot loaders/resources. Sources remain unchanged. The reference
uses matching classpaths, heap caps, JaCoCo versions/includes, and
`output=none,dumponexit=false`. It computes no per-method survival coverage.
This adapter is the accommodation for frozen snapshot collisions, not a timing
of the target project's ordinary current test suite.

Profiling and fixes before final measurements:

- Coverage analysis repeatedly read every production class for each invocation.
  [Stack samples](affected-selection-verification/coverage-profile-before.json)
  identified filesystem reads in `Analyzer.analyzeAll`. The worker now caches
  immutable class bytes once per JVM, analyzes only scope files for survival
  lines, and still collects hits for all production classes. Full pool coverage
  retains its full production analysis.
- A progress audit reread the same carried-origin batch for each method. The
  bounded 32-batch cache reduced an identical audit from **33.600 s / 3,848 JSON
  reads** to **1.283 s / 242 reads**, preserving all 48 completed indices.
  Replacement/deletion invalidates the cache. See
  [profile](affected-selection-verification/progress-origin-cache.json).
- The image-runner teardown fix and remaining exceptions are quantified above.

## Developer reset and fill policies

Both modes successfully run `survival policy plan` and `survival policy run`
with developer-only groups, a release reset at index 2 and threshold fill at
index 10. The independent regeneration pools match exactly (15 and 18 methods).
No model is used. Both policies have **zero differences** in per-method states,
first failures, survival tables, changed-line coverage tables, and
`coverage-missing.csv`. See [policy comparison](affected-selection-verification/policy-comparison.json).

| Policy / developer group | All executed | Affected executed | Affected carried |
|---|---:|---:|---:|
| Reset | 118 | 85 | 33 |
| Fill | 231 | 177 | 54 |
| Logical total | 349 | 262 | 87 |

These are logical per-policy method/index counts, including baseline evidence
used by segment 1. The same method identity is counted once across evaluation
and regeneration phases at an index; old/new snapshot identities remain distinct.
Shared physical work executes **231 methods** in all mode and **182** in affected
mode, a **21.21%** reduction. Per-index/group logical counts are retained in
[selection counts](affected-selection-verification/policy-selection-counts.json).

| Stage, seconds | All | Affected |
|---|---:|---:|
| Plan CLI | 0.211 | 0.207 |
| Pool preparation | 399.521 | 386.886 |
| Survival, including production builds | 357.465 | 413.158 |
| Coverage aggregation | 0.049 | 0.155 |
| Report | 0.010 | 0.004 |
| Whole run CLI | 759.852 | 805.001 |

Affected policy survival is **15.58% slower** here. Compilation falls from
47.171 s to 16.386 s, but test-JVM time stays nearly flat (186.195 / 187.408 s):
the expensive parameterized suite invocations still execute. Other work rises
from 83.789 s to 169.238 s, including resolved-classpath/dependency collection
and per-policy selection/projection. This small policy workload does not recover
that overhead through carried methods. The baseline-only 1.5× target does not
apply to regenerated policy tests.

Peak physical test-JVM RSS is **399.75 MiB** for all and **426.62 MiB** for
affected. All 16 evaluated indices have four-way wall-time breakdowns in
[all policy timings](affected-selection-verification/all-policies-per-index-performance.csv)
and [affected policy timings](affected-selection-verification/affected-policies-per-index-performance.csv).
These describe shared physical runs, not an artificial division of time between
policies. Production time sums Maven build attempts executed during survival;
checkout/classpath work is in other time. Pool builds are excluded, and anchor
restoration is charged to the first physical index. At regeneration indices,
evaluation and boundary work are both included. Late skipped-index projections
are excluded from interval boundaries. All phase sums reconcile and all residuals
are nonnegative; see [phase totals](affected-selection-verification/policy-phase-summary.json).

The final scratch resume probe found and fixed two issues: a single-pass progress
iterator omitted carried counts, and replaying unchanged cached compile failures
incorrectly added a remeasurement marker, creating false missing-coverage rows.
The repeated probe completes in **10.697 s**: it restores the identical removed
logical row, preserves **1,672 pool files**, **100 physical records**, and **14
CSV tables**, and reports **349 methods completed (262 executed, 87 carried)**.
See [resume/progress evidence](affected-selection-verification/policy-progress-recheck.json).
The fixes change progress and cached replay only; fresh benchmark decisions and
final selection ledgers remain unchanged. Original benchmark logs retain the
old progress undercount as historical evidence.

## Lifecycle, readers, and automated tests

The benchmark revision's validation covers **870 tests**: 854 Python
tests on both Python 3.12 and 3.14, plus all 16 real Java 25 fixtures, with zero
Java skips, failures, or errors. The fixtures cover
runtime reflection outside scope, discovery-only parameter-provider hits,
test-helper bytecode closure and source constants, nested classes, unknown
mappings, selection triggers and non-triggers, endpoint renames, segment/control
history, carried lines, invalid origins, iterative compilation beyond 100 errors,
concurrent resource-isolated chunks, and nonzero JVM memory evidence.

The performance fixtures check shared-compilation deduplication, wall component
reconciliation, full-execution ratio eligibility, and both measurements at a
policy regeneration boundary. Late skipped-index projections are excluded from
evaluated-index timing spans. Harness regressions preserve the DNS retry
settings and prior successful preparation timings when resuming an interrupted
comparison. The Java workflow explicitly includes the affected-selection
fixtures and rejects missing/skipped cases.

The [affected lifecycle fixture](affected-selection-verification/lifecycle.json)
uses real Docker workers, `survival stop`, status while running/stopped, and
resume. Resumed survival, first-failure, and changed-line tables are byte-identical
to the uninterrupted run. Developer reset/keep policies and completed policy
resume succeed without changing pools. A separate
[missing-decision repair](affected-selection-verification/policy-repair.json)
reconstructs an identical logical row in 8.355 s and leaves every pool unchanged.

Read-only `status` and `show report` succeed on the original finished DNS
experiment. Status reports all 179 builds, 178 survival commits, and 148 coverage
indices completed without rewriting historical hashes; the current-code
[reader output is retained](affected-selection-verification/finished-readers.json).
Old pre-framework JSV
inputs have no `experiment.yaml`, as before; policy readers use the existing
baseline-config fallback for those inputs.

## Revisions, retained attempts, and reproduction

JSV final control/survival and DNS affected use `05618bd`. DNS all uses `6adc038`;
the only later measurement-path change before its counterpart was the carried-
origin progress cache, which executed-only all rows do not use. JSV all retained
preparation/import/screening at `b6880b4`; DNS preparation uses `9f190c7` (the
unchanged legacy per-method scope selector), with screening at `6adc038`.
Exact stage revisions and commands are in the comparison JSONs. The final
`cfa6170` image-runner teardown change is verified by separate fresh controls;
it changes no compilation, selection, test-JVM command, or result semantics.
Policy runs use that revision. The final `9b22d82` progress-iterator and
`d8bd14a` cached-replay fixes are verified by the separate scratch resume probe;
they change neither fresh physical measurements nor fresh logical decisions.

Stopped attempts remain in scratch and contribute no final comparison totals:
initial classpath parsing with Maven ANSI/optional decorations; a DNS preparation
attempt that incorrectly disabled original retry settings; contended JSV timings
and references; pre-optimization policy/affected runs. The corrected DNS
configuration retains original retries. Reference setup failures (an uncached
provider and a noncanonical tool mount path) occurred before the reported timed
commands. A control-recheck setup attempt without prerequisite stage records
likewise failed before measuring tests. No original finished experiment was
rerun, migrated, pruned, or written.

Use fresh roots and a pinned tool checkout. All verification entrypoints reject
or preserve completed evidence rather than silently retiming it:

```sh
.venv/bin/python scripts/affected_selection_compare.py setup --root /tmp/jsv-check
.venv/bin/python scripts/affected_selection_compare.py all --root /tmp/jsv-check --tool /path/to/tool
.venv/bin/python scripts/affected_selection_compare.py affected --root /tmp/jsv-check --tool /path/to/tool
.venv/bin/python scripts/affected_selection_compare.py compare --root /tmp/jsv-check
# Repeat setup/all/affected/compare with --project dnsjava at setup and a fresh root.
.venv/bin/python scripts/affected_selection_performance.py reference --root /tmp/jsv-check --mode all
.venv/bin/python scripts/affected_selection_performance.py reference --root /tmp/jsv-check --mode affected
.venv/bin/python scripts/affected_selection_performance.py timing --root /tmp/jsv-check
.venv/bin/python scripts/affected_selection_compare.py policy-all --root /tmp/jsv-check --tool /path/to/tool
.venv/bin/python scripts/affected_selection_compare.py policy-affected --root /tmp/jsv-check --tool /path/to/tool
.venv/bin/python scripts/affected_selection_compare.py policy-compare --root /tmp/jsv-check
.venv/bin/python scripts/affected_selection_performance.py policy-timing --root /tmp/jsv-check
.venv/bin/python scripts/affected_selection_control_recheck.py --root /tmp/jsv-check --tool /path/to/tool
```

References and measured stages should run serially. Each mode normally gets a
separate Maven volume seeded from `docker_ktester-m2` mounted read-only. DNS
affected shares all's Maven volume and validated anchor method-coverage cache;
no cache fingerprints are rewritten. Reference volumes are separate copies.
The actual roots are `/tmp/affected-selection-jsv-optimized` and
`/tmp/affected-selection-dns-exact`. The helper-fallback reference is at
`/tmp/affected-selection-optimized-fallback`, and the original before-change
batch timing is retained at `/tmp/affected-selection-before`.

## PR review follow-up

Both review findings were reproduced in disposable fixtures before the fixes.
The new Java fixture failed because a later nested test lacked the production
dependency reached reflectively by its outer `@BeforeAll`. Two affected policy
coverage cases failed because identical class/method names from different
snapshots borrowed the first matching row. The equivalent all-mode coverage
cases already passed.

Runtime dependency capture now retains setup hits by test-container identity and
inherits them through every ancestor. The Java regression covers sibling and
deeper nested classes, parameterized descendants, and isolation from sibling
test-body hits and unrelated roots. After changing only the reflectively reached
production file, affected selection executes all four dependent methods, carries
the unrelated method, and matches all-mode verdicts. Dependency records are now
version 3; older records remain readable, but cannot authorize carries or satisfy
affected policy execution requests. Resume and physical/baseline reuse fixtures
verify that fresh evidence is required and an outdated carried pass cannot
override a newly observed failure.

Affected logical coverage now matches execution hash plus class and method. The
regressions verify both snapshots' line union, reversed row order, carried lines,
and missing-evidence reporting when one snapshot's row is absent. Digest-scoped
all-mode records retain their existing lookup behavior.

The follow-up full suite passes **879 tests** on Python 3.12: **862 Python tests
and all 17 real Java 25 fixtures**, with no skips, in 153.44 s. The nested-setup
fixture is included in CI's required fixture list.

The large scratch experiment comparisons and performance measurements were not
rerun for these review fixes. Their recorded revisions, counts, and timings above
remain historical benchmark evidence. No original experiment was modified and
no model calls were made.

### Second review follow-up

The next review identified lazy class initialization after the setup snapshot
and repeated logical-record parsing during policy coverage projection. Disposable
regressions reproduced both before the fixes: later methods lacked a dependency
reached reflectively by a test helper or production class initializer, and each
of two policies read its 1,000-method logical record 1,000 times at one index.

Affected JVMs now install initializer hooks after the JaCoCo transformer. The
hooks do not reset probes or change JaCoCo's class IDs, scope-line attribution,
or frozen class files. Production hits at initializer completion remain
available to later methods in that snapshot. This is deliberately conservative:
hits already present before the initializer may be reused inside it, so they
cannot safely be subtracted. Such extra dependencies may cause extra executions.
Failed/incomplete initialization or an isolated loader that cannot access the
hook bridge produces unbounded evidence rather than changing test behavior.
Dependency version 4 prevents reuse of earlier incomplete evidence.

Real Java fixtures cover test and production initializers, caught initialization
failure, isolated loaders, snapshot isolation, and exclusion of subsequent
test-body hits from inherited initializer dependencies. Changing only the hidden
production dependency selects both consumers and matches all-mode states and
scope lines, including lines inside a production initializer. Existing nested
setup, discovery, and concurrent snapshot fixtures remain part of validation.

Coverage projection loads each logical record once per policy/group/index and
indexes rows by snapshot/method identity. It avoids discarded physical-record
reads in affected mode; physical records in all mode are also indexed once.
The 1,000-method regression checks bounded read and key-lookup counts, complete
line coverage, and absence of spurious missing-coverage rows.

On one quiet-host projection-only measurement (two policies, one index, 1,000
methods per policy, 100 lines per method), wall time drops from **33.992 s** at
`cdf3f46` to **0.051 s** with the fix. Logical-file reads drop from **2,000 to 2**;
key evaluations are **4,000** after the fix. This is a disposable fixture timing,
not a new large-experiment survival benchmark.

The full local suite passes **885 tests** on Python 3.12 in **189.46 s**: **864
Python tests and all 21 real Java 25 fixtures**, with no skips, failures, or
errors. CI's required-fixture list includes all four new initialization cases.

The large experiment benchmarks were not rerun for this second review follow-up;
their timings and executed/carried counts above describe their recorded revisions,
not the new initializer instrumentation. No original experiments were modified.
