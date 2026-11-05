# Affected-test selection: implementation map

Part 1 only. Reviewed against `main` at
`d2f5a1b2e6dcfb8a65437054ed26bbe61bdcedac`, the merge of batch baseline
PR [#32](https://github.com/garibong-labs/test-survival/pull/32) on
2026-11-05. Branch: `feat/affected-selection`. This is a proposed design,
not an implementation or a claim that the equality checks have passed.
No experiment, policy run, pool construction, model call, or archive operation
was performed for this review. Finished target experiments remain read-only.

**Accepted compilation addendum:** Part 2 will replace selected-file-plus-all-
helpers fallback with iterative removal of javac-error source files, compiling
the remaining root together. This applies to both batch baseline and policy
execution, for fresh `all` and `affected` runs. It supersedes this review's
original selected-source-closure proposal and resolves the compilation design
decision previously left open. Part 1 was subsequently approved; the decisions below govern Part 2.

**Approved Part 2 decisions (superseding earlier alternatives):** affected
compiles executed test files plus non-test helpers, while all compiles the eligible
root; both use iterative error-file removal. Affected indices run in order with
concurrent chunks bounded by `runner.workers.survive`. Compiler invalidation uses
repository POM parent versions, compiler-plugin configuration, and properties
matching compiler/release/source/target/java.version, without an effective Maven
model. Historical evidence must remain readable, but exact resume fingerprints
need not be preserved. Dependency-bearing control at t can seed the first segment.
Fill-boundary executions, per-policy logical decisions, developer-only no-model
policy comparisons, and conservative execution with missing evidence are accepted.
Verification must report peak JVM memory as well as counts and timings.

The public setting is `survival.selection: all | affected`, default `all`.
Batch baselines and policy survival use affected selection; preparation,
denominator selection, screening, and pool construction retain their purposes
and configured repeat counts. For baseline execution, reject
`execution: legacy` with `selection: affected` rather than silently ignoring
selection. Policy execution already uses the batch worker independently of the
baseline's execution mode and can consume a legacy baseline read-only.

## 1. Existing execution and evidence paths

All paths below are repository-relative unless identified as experiment paths.

| Layer | Current implementation | Affected-selection insertion point |
|---|---|---|
| Batch baseline | [`stages/batch.py`](../src/test_survival/stages/batch.py): `catalog`, `Measurements.measure/cached/project`, `control`, `survive` | Partition the chronological alive entrants into execute/carry before building the worker's `methods`; merge carried evidence with returned observations before group projection. `control` measures t; its dependency-bearing execution may seed carrying at the first later index. |
| Policy survival | [`policy/execute.py`](../src/test_survival/policy/execute.py): `Runs.register/seed_baseline/run_index/prefetch`, `survive` | Compute demand per policy/group/segment, then deduplicate the required executions by snapshot identity and method. Preserve a logical selection decision per consumer even when physical execution is shared. |
| Shared worker | [`adapters/java_worker.py`](../src/test_survival/adapters/java_worker.py) routes `policy-survive` to [`adapters/policy_coverage.py`](../src/test_survival/adapters/policy_coverage.py): `run(w, job)` with `job['survival']=True` | Both callers use this worker. Add an opt-in dependency collection contract and return build-context identity with observations. There are not separate Java baseline and policy engines to extend. |
| Frozen sources | Baseline `catalog()` and [`policy/artifacts.py`](../src/test_survival/policy/artifacts.py): `snapshot`, `tree_hash` | Retain `execution_hash`, source-tree and resource-tree hashes. Identify a method by `(execution_hash, class, method)`, not its possibly reused class name or storage path. |
| Compilation | [`adapters/snapshot_compile.py`](../src/test_survival/adapters/snapshot_compile.py): `filtered_roots`, `compile_snapshot` | Filter screened-out sources as today; compile each demanded snapshot root once, then iteratively remove error files and recompile the remaining root together. Share the final classes and per-file removals across that root's selected methods. A root with no executed methods never enters snapshot compilation. |
| Test JVM | [`adapters/java/PerTestCoverage.java`](../src/test_survival/adapters/java/PerTestCoverage.java): `batch`, `executionFinished` | Derive runtime class hits from the same per-invocation probe dump as the verdict and scope lines. Keep isolated snapshot loaders and bounded chunks. |
| Coverage projection | `batch.project_coverage`, [`stages/cover.py`](../src/test_survival/stages/cover.py): `aggregate`, [`policy/cover.py`](../src/test_survival/policy/cover.py): `run` | Consume the merged rows, including carried lines; do not launch tests from a coverage reader. Retain existing population rules and executable universes. |

`policy_coverage.run` currently batches at most eight manifest groups per JVM,
groups by source root and execution hash, and runs methods sequentially in each
isolated loader. Its failed-JVM fallback calls `run_file` without instrumentation
and deliberately drops lines. Batch baseline requests `scope_universe=True`;
policy coverage gets the baseline's durable universe instead.

### Accepted compilation fallback

For each snapshot root with execution demand at the current index:

1. In all mode compile the whole eligible frozen root once. In affected mode,
   start with executed test files plus non-test helpers and allow javac to resolve
   referenced test-tree sources. In both modes use `-Xmaxerrs` high enough to
   avoid javac's default diagnostic cap (use the maximum supported value and
   verify a fixture with more than 100 errors). Preserve the complete diagnostic
   stream and each diagnostic's source-file association.
2. If compilation fails, remove **every source file with an error diagnostic in
   that attempt** from the compilation input, whether a test or a helper. Compile
   all remaining files together again. Repeat until compilation succeeds or no
   source files remain. This is a root-level retry sequence, not one fallback
   invocation per selected test file. Remove all error files reported in a round
   simultaneously; do not choose a preferred culprit or restore removed files.
3. A selected method receives `compile-fail` only when its own source file was
   removed. All methods declared in a removed file, including nested test
   classes, get that file's diagnostics. Methods in retained files run against
   the final successful output. Removing an unrelated helper must not assign
   `compile-fail` to the rest of the root.
4. Reflection-only and service-loaded helpers remain in the root while they
   compile. A failing helper is removed by exactly the same rule as a failing
   test file. A retained test that subsequently fails to load that helper gets
   the actual runtime result, not a fabricated compilation failure.

Use a worker-owned scratch copy of the eligible root. Removing a file means
excluding it there, never deleting a frozen source, pool member, or imported
artifact. Keep the original source-path/hash mapping for every file. Restrict
`-sourcepath` to the remaining scratch files so javac cannot implicitly rediscover
a removed source. Each retry uses a fresh class output directory; discard all
partial classes from unsuccessful attempts, including classes emitted from
removed sources. Only the successful output reaches the test classloader or
static dependency analysis.

Store the command (including `-Xmaxerrs`), input files, exit/timeout status,
duration, complete log, removed files, and each removed file's error diagnostics
for every round. Keep these per-file diagnostics even if method messages are
truncated. Prefer structured compiler diagnostics or a verified machine-readable
javac diagnostic format; do not mistake warnings, diagnostic continuation lines,
or mentions of another file inside a message for that file's own error. If a
failed compile has no attributable error file (for example timeout, OOM, invalid
flags, or an unmapped diagnostic), stop as an infrastructure/analysis failure
with evidence; do not loop without progress or mark the entire root compile-fail.
Every normal failed round strictly shrinks the input set, bounding the loop by
the initial number of files; an empty root launches no tests.

`policy_coverage.run` currently caches the first `full_suite=True` attempt and
then calls `compile_snapshot` again per selected file. Replace that split with
one cached root result containing final classes and a removal map. Construct
selectors only for retained test sources and reject only methods whose source
is in the removal map, even when the final retry succeeds. Audit shared callers,
including the `full_suite` path and `run_file` fallback, so neither bypasses the
new algorithm or reintroduces the old all-helpers failure. Preserve isolation
between snapshot roots and their distinct classloaders. Completed pool and
measurement artifacts remain immutable; the changed algorithm applies to new
compilations, not an automatic repair of finished experiments.

The approved affected compile set includes executed test files and non-test
helpers, rather than every unselected test in the root. Referenced test sources
remain available to javac until removed by an error diagnostic. All mode starts
with the eligible whole root. A root with no executed methods is not compiled.
Carried methods keep their recorded state; compilation removals are evidence,
not executions of unselected methods. Equality checks validate the requested
all/affected verdict parity under these boundaries.

## 2. Dependency production and cost

Dependency collection belongs to actual affected survival executions, including
baseline control and policy regeneration measurements. `all` and legacy rows
remain valid observations but are not selection certificates. This follows the
requirement that records written in those modes force execution when affected
selection first needs them. Do not retrofit dependencies into finished records.

### Runtime classes

Today `includes_pattern(scope_classes)` instruments scope packages, which is
insufficient: executed production classes outside those packages are invisible.
For affected jobs, derive the instrumentation include list from **all production
binary classes**, including nested, local, and anonymous classes, rather than
from scope classes. Avoid a global `*` that instruments third-party libraries.
Keep the line filter restricted to `scope_files`.

In `PerTestCoverage.executionFinished`, read each execution-data entry's boolean
probes and retain the binary class name if any probe is set. Do this before
discarding the in-memory dump. Join names to the commit's production inventory;
do not derive dependencies from covered scope lines. Write only a compact
class/file set alongside the existing scope TSV; union parameterized/repeated
invocations by `(class, method)` in `policy_coverage.run`. Do not write `.exec`,
full-production line TSVs, or XML for this path. The current survival scope flag
already suppresses `.exec`; extending instrumentation must not unset it.

Probe attribution needs fixtures for class initialization, `BeforeAll`,
`BeforeEach`, teardown, inherited tests, and parameterized containers. The
current listener resets probes at plan start and on every finished identifier,
including non-test containers. Shared setup hits must be conservatively attached
to the methods they support, and final teardown hits must not leak into the next
snapshot. A bounded per-container class-hit set can retain this information;
it does not require retaining production line coverage. Missing or incomplete
probe collection must never become a bounded empty dependency set.

### Static bytecode references

After successful compilation, analyze the **actual compiled snapshot output**
and the same classpath used for execution. A proposed small classfile reader
(or an explicitly configured `jdeps` adapter) supplies class-reference edges.
Start from the selected test class and its nested/local/anonymous classes;
walk references transitively through test-tree classes, including superclasses,
interfaces, providers, and nested classes of reached helpers. Record edges to
production classes, but stop traversal at production and external classes.
Do not replace this with a transitive production call graph.

Analyze once per distinct compiled output/context, not once per method.
All methods of the same test class may share this conservative static set.
Account for descriptors, signatures, annotations, class literals, and bootstrap
references; unresolved project/test-tree references make the result unbounded.
`DependencyInventory2.java` is a syntax/API inventory used elsewhere, not an
existing compiled-reference graph. `SourceInventory.java` alone is likewise
insufficient. Neither should be represented as already providing this feature.

The accepted fallback iteratively removes error files from the mode-specific input set;
it does not require a source dependency graph to prune helpers before compiling.
Build the static dependency graph from the final successful output, retaining
the removal map so missing project/test-tree references are not misclassified as
external. Cache graphs only with the source snapshot, retained/removed file set,
compiler, and classpath context that made them valid. Previously compiled bytecode
is dependency evidence, not a substitute for the current root compilation.

### Source-name references and class-to-file mapping

Tokenize the selected test source file and every test-tree source file reached
by the static traversal. Match Java identifiers to a reverse index of production
simple class names. Use a Java-aware tokenizer/parser, accounting for Unicode
escapes and excluding comments/string literals; substring searches are not
identifier matching. Ambiguous simple names contribute **all** matching files.
This captures inlined constants that disappear from compiled references.
Cache identifier sets by frozen source content hash; repeat the join against
the production inventory at the execution commit.

`JavaMavenAdapter.list_production_methods(index)` invokes
[`SourceInventory.java`](../src/test_survival/adapters/java/SourceInventory.java).
`results/source-inventory.json` describes the anchor, not every later commit.
Produce a per-executed-commit inventory beneath the new experiment, normalized
relative to that commit's module. The existing adapter `inventory()` strips the
anchor checkout prefix; later-commit paths need explicit normalization against
their own checkout. Parsing can be cached by production-tree identity.

Join source declarations with binary `SourceFile`/enclosing metadata. Named
member types are already inventoried with `$`; the current syntax scanner does
not enumerate method-local/anonymous classes. Map those binary classes to the
inventory's enclosing compilation unit, including non-public top-level classes
whose names differ from the filename. Blindly replacing `$...` and appending
`.java` is not sufficient. Handle loaded multi-release class variants without
analyzing duplicate binary names as one class.

Persist the sorted union of runtime, static, and source-name files as
module-relative `src/main/java/...` paths. Any depended-on project production
class without a verified mapping under that root makes the record `unbounded`,
with diagnostic reasons. This includes generated production outside that tree
and unsupported source layouts. Known JDK/third-party classes are classified as
external, not spuriously marked unmappable production; their changes are covered
by the context trigger. Missing graph/inventory evidence is conservative, never
proof of no dependencies.

### Expected overhead (estimates, not measurements)

Let P be the number of production probes, C production classes, B compiled test
bytecode size, E test-tree reference edges, S frozen source bytes, D the average
dependency-file count, and L existing scope-line evidence per method.

| Work | Time | Live memory / persisted evidence |
|---|---|---|
| Broader runtime instrumentation | Extra class transformation and probe writes outside scope; scan up to O(P) probes per invocation | Agent probe arrays plus transient dump O(P), hit names O(C); only O(D) dependency paths and existing O(L) lines persist per method. No retained matrix of probes by method. |
| Production inventory and binary/source map | O(source + class bytes) per changed production tree | O(C) map, shared across methods; durable compact mapping/context metadata. |
| Static graph | O(B + E) per compiled snapshot; memoized reachability per test class | O(test classes + E), bounded to the active snapshot/chunk; not a graph per method. |
| Compilation fallback | One full-root attempt on success; on failure, successively smaller root compilations, at most one failed removal round per initial file | One active compiler/output at a time; persist round logs and per-file errors. May save repeated per-file fallback compiles, but cascades can require several rounds. Measure rather than assume a speedup. |
| Source identifiers | O(S) once per frozen tree, then matching/reachability joins | Distinct identifier sets cached by source hash; union files stored, not repeated source text. |
| Selection | One Git endpoint diff per distinct `(last executed SHA, current SHA)`, then set intersections; one context extraction per commit | O(changed paths + active methods' D); direct pointers avoid chains proportional to commit count. |
| Build context | Artifact metadata lookup per commit and structural comparisons of changed POMs under the approved conservative rule | Small ordered artifact list per commit and cached endpoint POM comparisons. No effective-model extraction. |

The merged baseline Java listener analyzes the full production class directory
after each test before filtering scope lines. Extending probe inspection does
not require adding another full analysis. A later optimization can index scope
class bytes once and analyze only those for line output. Broad instrumentation
still increases agent heap and CPU; the original 40–58% execution estimate is
not a wall-time or memory guarantee. Keep chunking and report measured timings
and peak memory in scratch verification before claiming a speedup.

Part 2 implementation note: profiling found repeated directory traversal and
class-file reads in this analysis path. The listener now reads class bytes once
per isolated snapshot loader and retains only binaries whose `SourceFile` is
in scope for survival line analysis. Nested and sibling classes in the same
source file stay together. Full-suite pool coverage caches all production class
bytes. This adds heap proportional to those retained binaries, bounded by the
existing snapshot chunks, and removes repeated filesystem reads. Runtime class
hits still inspect all instrumented production probes independently of this
line-analysis cache.

## 3. Per-commit classpath and compiler context

The existing evidence is useful but does **not** yet satisfy the requested
comparison:

- `results/builds/<i>.json` records SHA, Maven compile command/attempts, module
  POM hash, classpath file reference, and resolution attempts.
- [`runner/cache.py`](../src/test_survival/runner/cache.py) keys the classpath
  cache by the module's own `pom.xml` bytes. Parent POMs, active profiles, Maven
  settings, and imported dependency management are not part of that key.
- [`maven_dependencies.resolve`](../src/test_survival/adapters/maven_dependencies.py)
  runs `dependency:build-classpath` with `includeScope=test`, which includes
  production and test dependencies. A cache hit checks that files exist.
- `java_worker.classpath(index)` prepends `target/classes` and the runner's
  manifest test jars, then appends project dependencies with the JUnit stack
  filtered out by `PROJECT_CLASSPATH_EXCLUDES`. Manifest order matters; runner
  jars can shadow project jars. JaCoCo/helper jars are added by the batch worker.
- `results/workspace-identities/<i>.json`, via
  [`workspace_identity.py`](../src/test_survival/adapters/workspace_identity.py),
  contains project dependency paths and content hashes for build restoration.
  It is not a resolved coordinate list and does not include all runner jars.
- Production uses `mvn -q [-o] -DskipTests compile`. Snapshot tests use direct
  `javac -cp ... -sourcepath ... -d ...`; no `--release`, `-source`, or `-target`
  is passed there. Thus tests currently use the runner JDK's defaults even when
  Maven production compilation targets an older release.

Add an affected-only, durable per-index context record, for example
`results/selection-context/<i>.json`. Build it in the execution environment from
the **final classpath actually supplied to the worker**, and retain:

1. Ordered third-party `(group, artifact, version, classifier, type)` entries,
   preserving order and any duplicates; exclude project output directories,
   snapshot outputs/resources, and the local helper output. Include runner
   libraries actually present. Join Maven-resolved artifact metadata to exact
   paths after worker exclusions, and manifest coordinates to runner libraries.
   Do not infer classifier/type from a filename alone or treat the configured
   `pom.xml` dependencies as the resolved list. Record content/path evidence
   separately for provenance, without confusing it with the requested key.
2. Instead of extracting an effective Maven model or compiler levels, compare
   every repository `pom.xml` between the last-execution and current SHA. Execute
   on a change to a parent version, compiler-plugin configuration, or property
   whose name contains `compiler`, `release`, `source`, `target`, or `java.version`.
   Include profile/execution settings and parse XML structurally. Cosmetic and
   unrelated POM changes alone are not triggers; unknown XML/diff evidence is.
   This conservative rule covers both production and test compilation as approved.

Unknown coordinates force execution; two unknown contexts are never equal.
Resolve once per evaluable commit before selection, even if every method might
be carried. A separate resolver action requires no test snapshot compilation.

For affected mode, bypass or strengthen the module-POM-only cache: resolving
afresh once per built commit is the simplest correct first version. Persist the
resolved list for subsequent method decisions. Recheck that the list supplied
to the JVM matches the selection context; if it changed, invalidate the carry
decisions and execute the affected demand. A cosmetic POM change with identical
resolved entries and relevant compiler POM fields is not a trigger. A parent-POM change
that changes a dependency is a trigger even if the module POM is unchanged.

Keep these new records separate from authoritative restoration identities and
old build records. Never rewrite finished experiments to add compiler metadata.

## 4. Selection state and chronological execution

Proposed method metadata, additional to existing state/message/lines:

```json
{
  "selection_action": "carried",
  "carried_from_index": 7,
  "last_executed_index": 7,
  "last_executed_sha": "<sha-at-7>",
  "dependency_record": {
    "version": 1,
    "selection": "affected",
    "files": ["src/main/java/example/Parser.java"],
    "unbounded": false,
    "context_sha256": "<resolved-context-digest>"
  }
}
```

An executed row uses `selection_action: executed` and its own index/SHA. Keep
the setting name separate from the existing record-level `execution: batch`.
The row's files, lines, and context describe its **last actual execution**;
carrying must not replace its context with the current commit's or advance its
last-executed pointer. `carried_from_index` points directly to the observation,
not the preceding carried row. Store the current row's index/SHA as today.
Segment eligibility belongs to the consuming policy/group/segment, not just the
shared physical method. Persist its binding or a deterministic selection ledger.

For every alive entrant at an evaluable index:

1. Execute without an eligible earlier affected record in this segment, with
   an unbounded record, or with missing/invalid dependency/context evidence.
   A dependency-bearing control execution at t is eligible for this segment.
2. Compare current context with that last execution's context; any changed
   resolved entry/order or relevant compiler POM field requires execution.
3. Diff the endpoint SHAs, not adjacent evaluated indices. Use
   `git diff --no-ext-diff --no-renames --name-only -z <last_sha> <sha_i>` and
   normalize repository paths against the module. Disabling rename detection
   exposes both old and new paths; `-z` avoids quoted/path-splitting mistakes.
   Execute on intersection with dependency files, or any change under module
   `src/main/` outside `src/main/java/`. This includes resources and other
   production source languages. Do not add a blanket POM trigger.
4. Otherwise copy the previous state and scope lines into a carried row.

The endpoint diff includes net changes across skipped and build-failed commits.
Do not advance last execution at either kind of unevaluated index. A change
fully reverted before the next evaluation has no endpoint diff, as specified.
Deaths remain cumulative; a later raw marker is not a new first failure.

`dispatch` currently permits multiple commits to execute before earlier deaths
are known, and policy `prefetch` runs a fixed-composition window ahead of its
consumer. Affected selection needs ordered publication and dependency updates.
For the initial implementation, serialize measurement indices in affected mode
while preserving parallel builds and independent pool work. Run JVM chunks at
one index concurrently, up to `runner.workers.survive`, with isolated resources. Keep current `all` scheduling. Record the effective scheduling choice
in comparison evidence. A later parallel implementation must wait for a method's
preceding decision or prove it independent; it cannot silently use stale records
and claim to execute only affected tests.

An all-carried index still needs the current build context and current scope
executable universe. Reuse the existing non-executing `--universe` analysis as
needed; unrelated scope files may have changed even though each carried test's
dependencies have not. No test snapshot is compiled, no JUnit JVM is launched,
and no coverage measurement is fabricated. Compiling the small analysis helper
is distinct from compiling a test snapshot.

## 5. Segments, fill, coverage repair, and resume

| Situation | Required handling |
|---|---|
| Initial baseline segment | Measure control at t normally. Control dependencies may seed selection history; legacy/all records cannot authorize carrying. |
| Policy segment 1 and `none` | Current policy code reads baseline verdicts/coverage directly. Preserve that reference behavior. To compare selection modes from the beginning, use the matching all/affected batch baselines; an affected policy over an all/legacy baseline does not retroactively select its segment 1. |
| Reset | New segment membership invalidates prior-segment selection eligibility, even when file bytes and execution hash recur. Pool screening is not an eligible survival execution. Execute new entrants at the regeneration commit. |
| Fill | `composition: carried` and `carried_alive` mean surviving members retained in the new composition, not methods skipped by selection. Under the literal “current segment” rule, retained survivors also need a measurement at the new boundary before later carrying. A real, compatible execution already performed at that same boundary can initialize the new segment; a row carried from an older index cannot. |
| Regeneration batch | The old composition is evaluated first. Batch the new segment demand after all policy compositions are known, deduplicating identical physical executions at this index. Keep old evaluation and regeneration evidence distinct (`<index>-regenerate` already exists); do not overwrite the row an old segment consumed. A later repair also must not change historical verdicts. |
| Same method in several policies | Keep per-consumer last-execution/segment bindings. One execution can satisfy several consumers that require it; a consumer allowed to carry keeps its own historical state/lines. A single shared row per `(digest,index)` cannot encode differing carry origins without an additional logical projection. |
| Carried coverage | Copy scope lines from the eligible actual execution into the current logical row. `batch.project_coverage` uses baseline alive-entering membership (including a test that dies at i); `policy.cover` uses its current post-transition alive membership. Preserve both rules and existing skipped-index aliases for pre-image coverage. |
| Missing coverage | `rules.coverage.usable_lines` rejects non-executing repairs via `remeasured_state`; reuse, progress, `Runs.track_missing`, and `coverage-missing.csv` must keep sharing that rule. Do not turn a missing measurement into a bounded empty dependency set or a carried zero. |
| Remeasurement | `settle()` preserves previously consumed state/message/flaky evidence. A successful repair can supply new lines/dependencies from its actual execution; preserve the original verdict. A disagreeing repair must remain explicit and should not authorize future carrying as if verdict and dependency evidence were one consistent observation; keep it conservatively ineligible. `absent`/`compile-fail` repair attempts cannot supply runtime dependencies. |
| Legacy/all imports | Read-only observations may still seed verdicts/coverage under existing compatibility rules, but cannot seed affected selection history. Separate observation reuse from dependency-certificate reuse. Execute when required to establish a local certificate; do not rewrite the imported source. |
| Skipped/build-failed commits | Preserve `evaluated=no` and existing unavailable-coverage semantics. These are not selection carries at an evaluable index and must not reset history. Existing compact skip markers/empty policy records need no invented method execution. |
| Resume | Replay chronological records and segment bindings to reconstruct last actual execution, validate source/context identity and direct origins, then reuse complete merged projections. Interrupted batches or uncommitted carry decisions remain pending. Rebuild missing derived outputs from durable evidence without executing usable methods. |

At baseline, write the shared measurement and selection decisions atomically
before group projections; mark completion only after the expected projections
exist. At policy level, preserve `Steps` transactions, but validate logical
selection ledgers as well as shared physical runs. `Runs.get()` and coverage
readers remain read-only. A checksum or identity mismatch in a carry origin
cannot be ignored merely because the current row has a `lines` key.

`Measurements.completed_items/stage_complete`, `progress.py`, and policy step
reuse must use the same eligibility/completeness predicates. Both executed and
valid carried methods count as completed. Status must not resolve Maven, run
Git/build jobs to repair data, or write exclusion summaries; inspect the durable
selection context and decisions. Retain the current owner/lock/session protocol
so `survival stop` terminates added analysis/resolver subprocesses too. Resume
must not rebuild completed pools or mutate imported experiments.

## 6. Counts, outputs, and compatibility

Add per-index/group executed/carried counts to raw group projections and a
dedicated selection-count ledger; add per-group totals to baseline
`final-summary.json` in `report/consistency.py`. Policies currently publish
`summary.csv`/`consistency.md`, not a policy `final-summary.json`; add an
additive JSON summary for policy/group selection totals without changing tables.
Record control separately from post-anchor totals. Count one logical method
evaluation per index/group, not JVM invocations, parameter repetitions, or
repair retries. Add separate physical-work counters for shared batches and
repairs so wall-time comparisons are interpretable. Derived groups inherit
method decisions but are not extra physical launches.
At regeneration boundaries retain phase-specific decisions; the per-index/group
total counts each snapshot/method identity once, as executed if any required
phase executed it, otherwise carried. Policies have separate logical totals.

An `executed` selection decision means requested for evaluation, including a
result of `compile-fail` or `absent`; retain launch/compile evidence to show
whether the method body actually ran. Such a row cannot get a fabricated bounded
runtime record. Dead/not-selected inventory markers are neither executed nor
selection-carried and do not enter these counters. This distinction preserves
existing complete-inventory raw records while meeting the new two-way labeling
for alive method evaluations.

Do not change headers or meanings of `survival.csv`, `first-failures.csv`,
`changed-lines.csv`, or policy tables. Broad class-hit collection does not restore
batch `all`/`other` changed-line columns: they remain unavailable, as merged in
#32. Keep scope-only line output, pre-image coverage, failure classifications,
and zero-versus-unavailable distinctions. Read compressed logical artifacts
through `io.read_json/read_csv/result_exists/resolve_path/logical_glob`.

Compatibility is a reader contract, not a historical resume-hash migration.
Finished experiments remain readable through status, show, report consumers, and
policy baseline readers. Preserve their artifacts and recorded verdicts. Fresh
runs record the new compilation strategy, selection setting, and dependency
schema; do not pretend old compile failures came from the corrected algorithm.
No additional pinned historical hash mappings are required by the approved scope.
Current-run resume still checks implementation/source identity, carry origins,
usable lines, and completed projections. Policy invocation selection must override
the baseline setting for new segment execution. Completed pools remain reusable
within that run and are not rebuilt for coverage repair.

## 7. Conflicts and alternatives requiring attention in Part 2

1. **Compilation conflict resolved by the addendum.** Implement the root-level
   iterative error-file removal described in section 1 for both fresh selection
   modes and both execution consumers. Do not retain the old selected-file-plus-
   all-helpers fallback or implement the previously proposed source closure.
   Healthy reflection-only helpers stay compiled; broken helpers and tests are
   removed individually with evidence. This is an authorized fresh-run behavior
   change to `all`, while completed results remain readable and unchanged.
   Verify the three documented JSV developer methods against legacy and then
   compare corrected batch `all` against corrected batch `affected`. Neither
   equal aggregate counts nor a known cause excuses a remaining method/line
   mismatch from the acceptance check.
2. **Fill survivors crossing segments.** Existing regeneration skips methods
   already evaluated at that index, even if their rows lack usable lines. The
   new rule distinguishes an actual same-index execution from a selection carry.
   Proposed direction: bind actual boundary executions to the new segment and
   execute previously carried survivors once at the boundary. Retaining older
   dependencies across a fill boundary would relax “current segment” and needs
   an explicit change to the requested rule.
3. **Shared policy storage.** Current `runs/<digest>/<index>.json` assumes one
   observation for all consumers. Different segment histories may require
   different carried origins. Proposed direction: retain physical run records
   and add per-policy/group/segment logical decisions, with `assemble` and
   `policy.cover` resolving the appropriate row. Avoid adding segment IDs to the
   physical execution hash and needlessly duplicating Java work.
4. **Context evidence and cache coverage.** Existing POM hashes, dependency-byte
   hashes, and javac commands do not supply the full selection context. Add the
   resolved-classpath evidence and conservative POM comparison described above; do not use every
   POM edit as a shortcut trigger. Unverifiable contexts execute conservatively.
5. **Parallel windows.** Current speculative dispatch cannot always know the
   last actual execution. Serialize affected commit measurement first, document
   the effective worker limit, and compare like-for-like one-worker baselines.
6. **General equality is empirical, not guaranteed by this heuristic.** Dynamic
   resource enumeration, previously unexecuted dispatch targets, reflection via
   strings, or build-plugin transformations can change behavior without changing
   a recorded dependency file, artifact coordinate, or compiler level. Same-GAV
   mutable artifacts also escape coordinate-only comparison. The required
   checks test concrete slices; they do not prove soundness for arbitrary Java
   histories. Keep the specified rule and report every observed mismatch;
   broader invalidation (artifact bytes, build effects, dynamic discovery) is
   an explicit alternative if those cases arise, not an undocumented fallback.

## 8. Verification plan after confirmation

This section records the accepted verification plan. Part 1 was committed and
pushed before approval; Part 2 now implements and verifies it. Actual results
belong in the affected-selection verification report, not implied by this plan.

Unit and Java fixtures should cover every selection trigger and its non-trigger:
runtime classes outside scope; transitive test helpers without production graph
traversal; inlined constants; ambiguous simple names; nested/local/anonymous
mapping; unbounded/legacy records; additions/deletions/renames; module resources;
endpoint diffs across skips and failed builds; unchanged and changed resolved
classpath; production and test compiler levels; cosmetic and inherited POM
changes; carried lines and pre-images; no compilation of all-carried snapshots;
first post-t evaluation; reset/fill/same-source new segments; shared-policy
origins; remeasurement disagreement; missing-coverage repair; compressed records;
default fingerprints, read-only status, interruption, and deterministic resume.

Compilation fixtures must cover: a healthy root compiled once; multiple failing
files removed together; cascading errors discovered on a later retry; all files
removed; a removed helper with unrelated tests retained; a compiling reflection-
only helper still loadable; a removed reflection-only helper causing a runtime
failure rather than unrelated compile failures; multiple/nested test classes in
one removed source; more than 100 diagnostics; per-file diagnostics retained
through success; no stale class output or implicit source rediscovery; no-progress
and infrastructure failures; and one shared retry sequence across selected files
of the same root in both batch baseline and policy execution. Assert scratch-only
removals and unchanged frozen source bytes. Also test an all-carried root is not
compiled, and a carried method is not reclassified because compilation for a
different method removed its source file.

### Compilation fallback acceptance and timing

Use the full batch comparison configuration in
[`batch-baseline-verification.md`](batch-baseline-verification.md), including all
three groups, imported generated tests, and no model calls, in new scratch
experiments. Measure the compilation change separately from affected selection:

- **Before:** pin the pre-fallback tool revision and run batch `selection: all`
  (omission on the old revision), preserving its measurements and logs.
- **After:** use the new iterative fallback with batch `selection: all`, identical
  inputs, worker counts, image/JDK, timeout/heap settings, and equivalent initial
  dependency caches. Save the exact revision and final retained/removed sources.
- **Selection comparison:** run batch `selection: affected` with the same new
  compiler strategy; the corrected after/all run can serve as equality check 1's
  all reference. Do not attribute selection savings to the compiler change.

The three methods listed in
[`survival-differences.csv`](verification/batch-baseline/survival-differences.csv)
must have the legacy states at every listed index, not merely pass at index 10:

| Class under `com.networknt.schema` | Method |
|---|---|
| `PropertiesValidatorTest` | `testDoesNotThrowWhenApplyingDefaultPropertiesToNonObjects` |
| `TypeValidatorTest` | `testNumeicValues` |
| `TypeValidatorTest` | `testNonNumeicValues` |

The existing ledger has 33 cumulative differences across indices 10–20; legacy
is `pass` for all of them. Verify first-failure histories too. Every developer
scope-line difference in
[`scope-line-differences.csv`](verification/batch-baseline/scope-line-differences.csv)
must disappear: 3,315 listed line/index differences, plus the changed-line
covered counts/percentages at indices 10, 13, 15, and 18. Check complete scope
universes and coverage availability, not only these totals, and enumerate any
remaining or newly introduced difference in every group with its cause. Retain
the legacy reference evidence; never rewrite the original ledgers to make the
comparison pass. Any legacy `error`/`timeout` normalization used by the original
cross-mode harness must stay explicit and cannot excuse a `compile-fail` or
`absent` difference. The subsequent batch-to-batch checks require exact states.

Report **survive-stage wall time before and after the fallback change**, its
absolute and percentage difference, and total wall time separately. The published
historical before value is **465.941 seconds (7m 45.941s)** for batch survival
(legacy survival was 461.384 seconds); these are recorded measurements from the
earlier comparison, not new measurements. Report the new paired scratch timings
alongside that historical reference, with compiler attempt counts, peak JVM RSS/heap-pool peaks, and surviving
populations: recovering three tests also adds later execution work, so a stage
timing difference is not purely compiler overhead. An after time and the claimed
disappearance of differences remain pending Part 2 execution.

### Affected-selection equality checks

For equality check 1, reuse the configuration in
[`scripts/batch_baseline_compare.py`](../scripts/batch_baseline_compare.py) and
[`docs/batch-baseline-verification.md`](batch-baseline-verification.md): JSV
anchor `0c14a7a8664334fd10108895f84d8232f6c34019`, 20 post-anchor commits ending
`51fd82b420c3abde014f180ea109155de0d36480`, explicit scope, developer/KTester/
Ouédraogo groups, imported generated sources, one worker, no model calls. Both
runs now use `execution: batch`; vary only selection and isolated output/cache
locations. Do not reuse the old legacy-versus-batch result as this comparison.

For equality check 2, clone dnsjava without shared writable worktrees, copy the
`experiments/commit-survival` configuration read-only, pin its resolved SHAs, and
evaluate indices **0 through 60 inclusive** in batch mode for its developer
scope group and Ouédraogo group. Import generated sources from that experiment;
do not run generation. Use separate all/affected experiments and record immutable
input hashes before/after, tool revision, JDK/image, and cache preparation.

For policy equality, use the check 1 baselines with one reset and one fill policy
at identical trigger points. The old comparison harness runs developer-only
policies only through pools and cannot satisfy this request unchanged. Extend
the scratch harness to full `policy plan` and `policy run`. For a no-model check,
use `policies.groups: [dev]` with developer regeneration, exercising both reset
and fill through later commits. If generator policy groups are included, seed
verified matching regeneration pools from available frozen inputs; the current
`build_pool` calls `generate` for generator groups. Importing baseline generated
tests alone does not make generator policy pool construction model-free.

Compare exact method states, chronological first failures, and changed-line
coverage/availability per index/group, including per-policy segment/event
identity. Expand carried rows through their observation origins, include
pre-image and per-index scope-line checks, and compare frozen populations and
scope universes. Both sides use batch semantics, so do not normalize differing
failure kinds away. Report every discrepancy and its cause, logical executed/
carried counts, physical/repair counts, and wall-clock times for each run and
stage. Preserve mismatch evidence even when the cause requires a design change.

After the checks, update README, regeneration-policies, result-formats, and
tool-settings documentation, push, and open one PR without merging. No finished
target experiment may be modified or deleted at any stage.
