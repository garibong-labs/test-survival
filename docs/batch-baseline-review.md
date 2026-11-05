# Batch baseline: implementation review and comparison plan

**Accepted Part 2 amendments:** batch coverage collects only scope source lines;
`all` and `other` changed-line columns are unavailable. Batch writes no group
JaCoCo XML and retains no probe data for XML generation. The comparison adds
KTester sources from the read-only `commit-survival-run2` and extends to 20
commits (subject to a one-hour limit per mode). These decisions supersede the
full-universe/XML proposals and four-commit comparison below, which are retained
as the Part 1 review record. Current behavior is documented in
[result formats](result-formats.md#batch-baseline-results).

Part 1 only. Reviewed against `main` at
`f9459540081a0749adbeca6b7c550bb097e60f1a`, after footprint PR
[#31](https://github.com/garibong-labs/test-survival/pull/31) merged on
2026-11-05. This document proposes implementation; it does not claim that batch
configuration is currently accepted or that the comparison has run. Finished
experiments were inspected read-only. No experiment, model, pool, or archive was
run for this review.

## Decisions for Part 2

- Add `survival.execution: legacy | batch`, defaulting to `legacy`. Omission must
  preserve existing stage, raw-record, import, and policy-input fingerprints.
  Batch must never accept legacy pass/fail-only evidence as a batch measurement.
- In batch mode, **ignore `survival.flaky_rerun` for baseline measurement**, and
  record that fact in measurement metadata and the final summary. This permits
  the requested `survival: {execution: batch}` configuration with the existing
  default `flaky_rerun: 1`. Batch measurements also disable environment-error
  reruns, matching policy execution. Preparation, denominator selection, and
  screening retain their own existing rules; the setting does not redesign them.
- Reuse the `policy-survive` worker and snapshot machinery, with a baseline
  output projection. Do not simply call legacy `run_group()` once per group.
  Submit the union of required methods per index, compile each source root once
  on the successful path, and partition observations back into baseline groups.
  Preserve the worker's failed-root compilation fallback and bounded JVM chunks.
- Measure index zero through the batch worker in `control`, validating every
  frozen member and saving coverage from that execution. `survive` measures
  subsequent evaluable indices. Keep `cover` as a **non-executing aggregation
  stage**, so CLI stage names, resume dependencies, and report consumers survive.
  Add the mode to control-stage identity: currently control hashes omit it.
- Keep the fixed denominator, cumulative deaths, and **alive-entering** baseline
  coverage rule. A method that dies at index N can contribute coverage at N;
  it contributes nothing after N. Do not substitute policy post-transition alive
  membership for this rule.
- Preserve complete production executable universes and group coverage in
  addition to reusable per-test scope hits. The current policy worker alone
  cannot produce the baseline `all` and `other` coverage partitions.
- Resolve developer behavior per policy, falling back to
  `policies.regenerate_developer`. Plan the union of actually requested developer
  points; execute and label developer references per policy.
- Use configured `dev.stable_runs` and `screen.stable_runs` in pools, including
  the currently hard-coded developer worker `repeats=3`. New funnels use
  `stable_runs` and `stable_passed`; readers accept old `stable_3_of_3` as a
  three-run stable count. Do not rewrite completed pools or generated archives.

## Baseline output and consumer inventory

Paths are relative to an experiment. They are **logical paths**: after #31,
bulk JSON, coverage CSV, and XML may physically be `.gz`. All new consumers must
use `io.read_json`, `read_csv`, `result_exists`, `resolve_path`, `logical_glob`,
and `result_hash` as appropriate. Do not enumerate only plain `*.json` files.
Small summaries and report inputs remain plain. The table includes upstream
baseline artifacts needed to construct or consume the measurements, not just the
files written by `survive`.

| Output / artifact family | Current producer and later reader | Batch production / compatibility obligation |
|---|---|---|
| `experiment.yaml`; `state/resolved-range.json` | Configuration and prepare; all stages, policy `Baseline`, import compatibility, dashboard | Retain original YAML/default semantics and resolved endpoint. Add mode only to affected identities; omitted mode means legacy, including old manifests. |
| `results/anchor.json`, `commits.csv`, `commit-paths.json`, `commits-check.md` | Prepare; scope, survive, cover, progress, report, policy baseline/plan, developer import | Same first-parent sequence, SHA/date/subject, build classification, scope change flags and key-presence metadata. No policy-local renumbering. Markdown remains a human build summary. |
| `results/builds/<i>.json`, `workspace-identities/<i>.json`, build command/log evidence; `build-history/`, restoration evidence | Adapter/prepare; worker setup, survive/cover eligibility, progress, policy `evaluable`, #31 restoration and cleanup | Preserve authoritative original build results, dependency identities, separate replacement/restoration histories. Skip records have no build attempts. Failed builds carry survival and unavailable coverage. |
| `results/source-inventory.json`; `dev/src-test-t/{java,resources,suite}` | Prepare; scope, screen/control, all measurements, policy snapshots, developer import | Keep the full frozen t suite, including reflection-only helpers and resources. Compile it once per root/index, not just selected developer files. Never replace it with that index's current suite. |
| `dev/dev-methods-all.csv`, `dev/dev-denominator.csv`, `dev/<group>-denominator.csv`, `<group>-excluded.csv`, `dev-unmapped-excluded.csv`; `results/baseline-unmapped.json`, `dev-group-selection.json` | Prepare/scope; screening, matching, import, selection notes and reports | Preparation/selection unchanged. Preserve configured stability runs, denominator rule, unmapped exclusions, group names/order, derived subsets, and selection-only groups. |
| `population/class-survey.csv`, `accessor-audit.csv`, `accessor-methods.tsv`, `scope.json`, `scope.md`, `methods.csv`, `ktester-population.csv`; `results/scope-selection.json` | Scope; generators, screening, coverage/report, policy baseline/plan/pool/snapshot unit mapping | Same scope and focal/replacement identities. The JSON/CSV carry machine inputs; audit/Markdown remain explanatory evidence. Explicit scope in the comparison avoids range-dependent ranking changes. |
| `results/dev-method-coverage-t.csv`, `dev-method-coverage-t/<class>#<method>.{json,xml}`, matching `-lines.csv` | Scope's coverage-scoped selection; selection, audit and imports | Distinct from group coverage at t. Leave selection semantics intact; the baseline mode does not silently replace these isolated selection measurements with batched membership. |
| `generated/<group>/round*/` sources, `generation.json`, validation records, funnels, `rounds.json`; generator completion records | Generate/screen; screen reuse, matching, progress, policy source/unit/exclusion mapping, final-summary model rounds, KTester report refresh | Reuse/copy generated evidence; no model calls in comparison. Preserve validation exclusions and `temp/` scratch exclusions before compiling snapshots. Read old funnel keys. Completed generation archives remain immutable inputs. |
| `results/llm-<group>-denominator.csv`, `frozen-denominators.json`, `denominators.json` | Screen; control/survive/cover, progress, all reports, developer imports, policy baseline/plan | Keep frozen IDs (`file\|class\|method`) and counts. Add execution/source/resource hashes to auxiliary batch identities without changing baseline IDs. Derive groups by method membership, never by copying parent aggregate coverage. |
| `results/control/0-<group>.json`, `control-at-t.json`, `control-at-t.md` | Control; screen-to-survive gate, summary/progress and developer imports | Project index-zero batch observations into compatible per-group control records. A control failure stops the pipeline; row zero remains the entire denominator, not a silently reduced set. Save t coverage during the same batch. |
| `results/skipped-commits.csv` | Survive; policy baseline/plan, summaries and report consistency | Same `index,sha,reason`. Use `rules.commits.skip_reason`: module `src/main/` plus **any** `pom.xml` anywhere in the repository, not only the module POM. |
| `results/raw/<i>-<group>.json`, `raw-derived/<i>-<group>.json` | Survive/derived projection; survival aggregation, first failures, report rerun counts, progress, imports, policy segment 1 and baseline alive sets | Full frozen inventory, actual requested IDs, state/message/flaky fields, fingerprints and compile/run evidence. Add `execution: batch`, coverage version/status/reason and per-test `lines` in policy-compatible coordinates. Skips keep compact marker records; build-failed indices keep the existing no-measured-raw convention. |
| Proposed `results/baseline-batches/<i>.json` and auxiliary batch coverage evidence | Shared worker; ordered projection, resume and diagnostic audit | One shared measurement record per evaluable index, with source/execution identities, run/chunk/compile evidence and coverage completeness. Group projections must link to this record and remain independently readable. Publish atomically; do not mark complete after only some group projections exist. |
| `results/survival.csv` | Survive aggregation; figures/tables/dashboard, policy baseline completeness check, threshold planning, `none`, developer imports | Same headers/order, row zero and all indexed commits. `evaluated=no` for skip/build failure; carry previous counts. Aggregate in commit order even when workers finish out of order. |
| `results/first-failures.csv` | Survive aggregation; breaking-commit table and policy `none` | Same fields, one chronological loss per method. Batch kinds are `fail`, `absent`, `compile-fail`; `flaky=false`. No synthetic `error`/`timeout` split for batch. |
| `results/alive-entering.json` | Survive aggregation; coverage membership, progress and imported coverage validation | Save exact chronological entry sets, including indices that carry state. Filter parallel speculative executions against these sets. |
| `results/survival-consistency.json`, `missing-survival-records.json`, quarantine records | Survive; report consistency/final summary and resume diagnostics | Preserve raw/derived expected counts, build-fail/skip lists and denominator checks. New job count is not the expected per-group raw-record count. Incomplete/aborted workers cannot be accepted as complete. |
| `results/coverage-commits.csv`, `coverage-index-aliases.json` | Cover; aggregate, progress, cleanup and legacy-layout audits | Retain `coverage_indices()` planning: anchor when requested, production-changing commits and preceding non-skipped indices. Batch records hits at every evaluable index; the reporting subset can remain unchanged. Alias chains must resolve before pre-image lookup. |
| `results/coverage/<i>-<group>-lines.csv` | Cover worker; changed-lines aggregation, universe checks, coverage-at-t table, policy cover executable universe, imports | Full production executable coordinates, `covered=yes/no`, filtered to exact alive entrants for that group. Generate from stored batch evidence without test execution. Empty groups still get a nonempty executable universe when production has executable lines. Missing coverage stays unavailable, not all `no`. |
| `results/coverage/<i>-<group>.xml` | Cover worker; retained JaCoCo evidence and developer-result imports | Retain genuine analyzer-produced JaCoCo XML under #31. A boolean line map cannot reconstruct instruction/branch counters: preserve sufficient per-member probe evidence through ordered aggregation, union only contributing members, and emit XML before releasing classes. Do not fabricate XML from `lines`. |
| `results/raw-coverage/<i>-<group>.json` | Cover worker; import validation, progress, report coverage-record counts | Write a projection/reference to the same measurement, with compatible index/SHA/method IDs/status and explicit `measurement_source`/mode. It must not claim a second run occurred. Keep real launch evidence linked or embedded. |
| `results/changes/<i>.json` | Cover; changed-lines aggregate and policy cover | Same first-parent diff, pre/post images, module-relative coordinates. Restrict images with each side's durable executable universe, resolving skipped predecessors. Git diff needs repository history but no built checkout. |
| `results/changed-lines.csv` | Cover aggregate; tables/exposure, policy baseline completion gate, policy cover/report including `none` | Preserve `all`, `scope`, `other`, covered counts/percentages, losses and pre-image coverage. Continue using entry-set coverage and blank unavailable cells. Keep zero executable distinct from missing measurement. |
| `results/coverage-consistency.json`, `coverage-universe-mismatches.json` | Cover; coverage/report completion, final summary, status summary | Preserve duplicate/universe checks, missing `(index,group)` records, identical sets and unavailable/zero counts. Add reasons for missing batch coverage rather than conflating it with build failure. |
| `results/table1-survival-by-event.md` through `table4-exposure-vs-break.md`, `coverage-t.md` | Report tables; README and human consumers | Existing renderers consume the same survival/failure/changed-line/line CSVs. Coverage at t uses index-zero group maps, not developer selection coverage. |
| `results/figure1-survival-by-commit.{svg,png}`, `figure-render.log`, `results/consistency.md`, `final-summary.json`, report section of experiment `README.md` | Report; users, dashboard where applicable, completion/archive tooling and later comparisons | Keep existing renderer/summary outputs, timings, denominators, model-round counts and availability status. Add execution mode and ignored retry settings. `phase_seconds.cover` now means aggregation, not Java re-execution. |
| `state/<stage>.json`, error files, run/lock/worker ownership, import manifests, progress events/logs | Pipeline/lifecycle/importer; resume, status/watch, stop, archive guards | Use existing lifecycle ownership. Reuse and progress must use the same batch completeness/fingerprint predicate; partial projections do not count complete. Preserve import checksums over uncompressed result bytes. |
| Compile-event journals, job inputs/logs, `results/work-cleanup*`, failure/history evidence | Workers/#31; audit, cleanup and restoration | Keep actual compile/run diagnostics and failed/interrupted shared batches. Register new baseline scratch roots with cleanup. Direct snapshot compiler attempts already live in batch `compile` evidence; do not invent legacy journal events that did not occur. |

The principal code paths establishing this inventory are
[`stages/survive.py`](../src/test_survival/stages/survive.py),
[`stages/cover.py`](../src/test_survival/stages/cover.py),
[`report/tables.py`](../src/test_survival/report/tables.py),
[`report/consistency.py`](../src/test_survival/report/consistency.py),
[`dev_results.py`](../src/test_survival/dev_results.py), and
[`policy/baseline.py`](../src/test_survival/policy/baseline.py).

## Behavior differences and output interpretation

| Concern | Legacy baseline now | Batch baseline proposal, following current policy execution |
|---|---|---|
| Job boundary | Per index/group, compile/run by file; successful developer whole-suite compile reused by files. | Union across groups per index; compile by snapshot root, per-source selectors/loaders. Shared batch plus compatible group raw projections. A normal test JVM chunk contains at most `MAX_SNAPSHOT_GROUPS=8` manifest groups; this is not necessarily eight distinct source roots. |
| Compilation failure | Developer whole-suite failure can still fall back to individual file compilation; generated files compile independently. | Failed root attempt is retained and reused by selected-file-plus-helper fallbacks. Do not set global `full_suite=true` on baseline survival: that would suppress these fallbacks and select whole classes. Raw methods receive `compile-fail` only when their relevant compilation fails. |
| JUnit outcomes | `pass`, `fail` (Assertion family), `error`, `timeout`, `absent`, `compile-fail`; skipped XML cases are absent. | All successful observed invocations => pass; any non-successful observed invocation => fail; no observation => absent. Aborted invocations can therefore differ from legacy skipped/absent. Parameterized invocations union by class/method. Keep exception-class-prefixed messages, truncated to 4,000 characters. |
| Loss classification | First-failure kind retains error/timeout distinctions. | Map legacy fail/error/timeout to fail only for comparison. Preserve other differences as findings; do not remap absent or compile-fail away. `dead`/`not-selected` are non-execution markers, not fresh observations. |
| Timeout and heap | Per-file launcher limit `runner.timeouts.test_class`; selector probes also have a limit; launcher heap plus SerialGC. | Chunk timeout is `test_class * number_of_selected_methods_in_chunk`; compile has its own timeout. Instrumented JVM uses `runner.jvm.jacoco`, optionally `SURVIVAL_JVM_JACOCO`, and records effective heap. Not equivalent to a per-method timeout. Preserve run exit/timeout fields and chunk labels. |
| Failed JVM / fallback | Per-file timeout/error classification; workers killed with -9/137 can quarantine raw survival records. | Failed/timed-out test chunks use per-file survival fallback with flaky and environment retries disabled; fallback maps errors/timeouts to fail and removes stale line hits. Partial successful chunks retain their hits. `coverage_status=missing` and reasons make unavailable coverage visible. Infrastructure failure before usable output still fails the stage. |
| Flaky reruns | Configured failing/error method reruns can rescue to pass with `flaky=true`; default one. Cover can rerun too. | Ignored for batch measurements, including fallback. Store `flaky=false`, mode and effective retry settings. Explicitly document this instead of rejecting the user's abbreviated target configuration. |
| Environment-error reruns | Pattern-matched failures may relaunch the class with configured backoff; not disabled merely by `flaky_rerun: 0`. | None in normal batch or fallback. Compare with empty patterns and zero max reruns in both modes. Maven build/dependency download retries remain their existing independent behavior. |
| Coverage timing and membership | Separate phase B runs exact alive entrants under JaCoCo, including methods that first fail at this index. It can observe behavior different from phase A. | Capture in survival/control pass, then project exact entry sets; no phase-B test launches. A parallel job may run a conservative superset while earlier deaths are pending, as current policy prefetch does. Discard those extra contributions during ordered aggregation. |
| Coverage scope | Legacy JaCoCo instruments broadly and reports all production executable lines, excluding multi-release duplicate class roots. | Current policy survival restricts instrumentation by scope packages and saves only scope-file lines. Extend the shared worker with an explicit baseline collection option: full production instrumentation/universe and reusable scope hits. Keep policy collection defaults intact. |
| Lifecycle probes / isolation | Whole-file `.exec` includes setup, teardown, and incidental execution across that process. | `PerTestCoverage` dumps/resets after each test and container event; container teardown probes are discarded. Static/shared state and class-loader/JVM grouping also differ. Save actual batch coverage, list any parity differences and causes, and never silently relabel batch coverage as legacy semantics. |
| Empty or unobserved population | CLI analyzer can emit uncovered executable lines with no execution data. | Current policy universe is accumulated while reading observed test outputs and can be empty when nothing executes. Baseline must analyze production classes independently to produce the full universe, even with an empty denominator or all compile failures. An empty `lines` list means measured zero/non-execution as classified; a missing `lines` field means missing coverage. |
| Anchor | Control executes all frozen methods; cover separately measures t when requested. | Batch control measures/validates t once and stores full evidence. Publish t line/XML projections if required by `coverage.at_anchor` **or** as a changed commit's predecessor. Do not drop row zero or bypass the control gate. |
| Skip/build failure | Skip carries counts and records `skipped-untouched`; build failure carries counts without measurements. Coverage aliases bypass skipped commits, not failed builds. | Same eligibility, records and aliases. Do not execute skipped commits to obtain coverage. A build-failed predecessor remains unavailable rather than aliasing to an older successful build. |
| Resume | Existing raw/provenance checks and completed stage fingerprints; skip/dead bookkeeping. | Require current mode, identities, inventory and coverage schema. Preserve consumed survival verdicts when refreshing lineless fallback rows, using policy `settle()`/`remeasured_state` rules. Coverage-only resume must not launch tests: missing measurement is handled at the measuring stage or reported unavailable. |
| Progress/stop | Stage panel/status, bounded dispatch, coordinator/worker/container ownership. | Keep the same locks and lifecycle. Report index completion only after valid batch and group artifacts; cached items count as complete without inflating fresh throughput. Stop drains/terminates owned workers; interrupted evidence remains for resume. |

Relevant implementation details are in
[`java_worker.py`](../src/test_survival/adapters/java_worker.py),
[`policy_coverage.py`](../src/test_survival/adapters/policy_coverage.py),
[`PerTestCoverage.java`](../src/test_survival/adapters/java/PerTestCoverage.java),
[`snapshot_compile.py`](../src/test_survival/adapters/snapshot_compile.py), and
[`policy/execute.py`](../src/test_survival/policy/execute.py).

### Reusable raw evidence and ordered coverage

Keep baseline IDs in group raw records. Add source-file, Java-tree, resource-tree
and execution hashes matching `policy.artifacts.snapshot()` (relative names plus
content, independent of storage path). Each actual method observation needs
`class`, `method`, state/message, `flaky=false`, and `lines: [[file, line], ...]`
for scope coordinates, with coverage schema metadata on its record. Keep full
production per-method hits separately for baseline group projection.

Policy segment 1 already reads `Baseline.raw()` directly and `none` already
copies baseline survival/report coverage; neither requires new test execution.
However, `Runs.run_index()` currently does **not** seed measured source records
from those baseline rows. Add identity-checked import/seeding there for matching
snapshots and indices, so carried baseline methods at a regeneration boundary
can reuse their hits. Never reuse an index-t screening hit at a different index,
nor a baseline hit for changed helper/resources. Legacy records without lines
remain readable and follow existing missing-coverage behavior.

For parallel execution, a dispatch-time alive superset is insufficient to write
final group coverage. After chronological survival aggregation, union only the
exact `alive-entering.json` members' hits/probes. Retain the batch's necessary
probe data and production classes until line and XML projections are durable and
validated. This also supports overlapping developer groups without contaminating
a child with its parent's extra members. XML generation/analyzer work may occur
after the test JVM; it must not execute tests. If complete probe evidence is
unavailable after fallback, emit unavailable coverage and the reason, not a
synthetic zero-coverage report.

## Developer regeneration and pool screening map

Today `policy.config.regenerates(b, group)` is global. It affects plan points,
active-plan filtering, pool scheduling/matching, coverage reference groups, and
report labels. `execute.survive()` builds per-policy points by trigger only.
Changing just the schema or planner would consequently regenerate developer
segments for a `keep` policy.

Proposed resolution:

1. Resolve each set entry's optional `developer` to `regenerate` or `keep`; when
   absent use the global boolean (default true). Keep `none` a baseline reference.
2. Planning includes developer release/threshold candidates if any policy
   regenerates developers, but pool demand must use requesting policies' triggers.
   A developer point requested by one reset policy must not apply to half-fill
   with `keep`. Record applicable policy names or compute this predicate centrally.
3. Execution points, segment transitions, coverage references, report/table labels
   and progress use the same resolved per-policy predicate. Keep means baseline
   developer survival and coverage throughout, even while other groups regenerate.
4. Matching first uses a developer pool at the same index when present, as
   requested. Otherwise determine each requesting policy's in-force developer
   denominator: fixed t denominator for keep, latest reset/fill denominator for
   regenerate. Take the largest requested target for a shared generator pool.
   Preserve the existing limitation requiring a same-index developer pool after
   multiple developer fill checkpoints when the target cannot be known at pool
   scheduling time. The supplied target configuration has no developer fill.
5. Pool configuration comes from `Baseline.execution_config` (the baseline
   experiment plus supported overrides). Remove both forced values in
   `pool_config()` **and** use configured `dev.stable_runs` in the developer
   screening job. Full-suite per-test coverage at the regeneration commit remains
   a separate selection pass, followed by the configured number of strict
   screening passes; it is not counted as an extra stability pass.
6. Generator funnels also originate in `stages.screen.screen_round()`, whose
   `stable_3_of_3` key is currently misleading even outside policy pools when the
   configured run count differs. Update the shared writer and all consumers to
   the explicit run-count schema with legacy fallback. Preserve per-prompt
   counts, deduplication and aggregate/list funnel shapes.
7. Separate aggregate execution revisions from immutable pool identities.
   `digest_options()` currently omits the global switch, while `Steps.revision`
   tracks it; adding resolved policy keys blindly would invalidate old plans and
   pools. Normalize absent per-policy fields to old semantics, revise composition
   outputs when behavior changes, and require deliberate plan rebuilding when
   enabling regeneration needs points absent from a saved plan. Never silently
   reuse a one-run pool as a three-run pool or recompute completed pools merely
   to refresh reports.

The fixture must call the real developer `build_pool` path, prove full-suite
coverage selection and configured repeats, publish a nonempty snapshot, open a
segment, and execute it at the boundary and a later index. Assert keep/reference
behavior beside regenerate behavior, shared matching targets, helper/resource
identity, skip carry, and reuse. A mock-only test that supplies `pool.json` does
not establish this end-to-end property. Extend the real Docker fixture from #31
and add focused unit fixtures for coverage-scoped selection, fallback/missing
coverage, mixed triggers, old funnels and legacy fingerprints.

## Small real-project comparison (Part 2 only)

Use the completed, pre-framework JSV experiment at
`~/_projects/experiment-projects/json-schema-validator/experiments/commit-survival`
as **read-only generated-source input**. Its final summary says `completed`.
It has no `experiment.yaml`; the generated importer supports discovery from
group directories in this layout. Read-only inspection found:

- Anchor `1.1.0`: `0c14a7a8664334fd10108895f84d8232f6c34019`.
- Five explicit scope classes listed below; 10 frozen developer methods and
  23 kept Ouédraogo methods in the old experiment. One Ouédraogo round contains
  25 candidate files; its whole generated tree is about 3.47 MB.
- Endpoint `5a94df7e914b90c185fb573b6e7a6b3fb5f2460b` is index 4.
  Indices 1 and 3 are documentation/test-only; 2 and 4 change production sources.
  Expected coverage indices are `[0,2,4]`, aliases `{"1":0,"3":2}`.
  The old curve loses three developer methods at index 4, so this slice includes
  a death event. Fresh screening is authoritative; do not assume identical
  denominators merely because the old counts are small.

Create a new clone outside all finished experiment directories, for example
`/tmp/batch-baseline-comparison/repo`, with two fresh sibling experiments
`experiments/legacy` and `experiments/batch`. Clone from the local target Git
repository without changing its branch or tags. In the scratch clone only,
create a tag `batch-comparison-release` at index 2
(`48ca8c3c4eb89e1d00ea0e8097daac187caff142`). This provides a guaranteed developer
pool checkpoint with a later evaluable index without extending the slice to a
real release. It is a test checkpoint, not a claim that upstream released there.

Complete common configuration (set `name` per destination; the block is legacy
until Part 2 adds `execution: batch` to the batch copy):

```yaml
name: batch-baseline-comparison-legacy
project:
  repo: /tmp/batch-baseline-comparison/repo
  language: java
  build: maven
  module: .
  test_framework: junit5
anchor: 0c14a7a8664334fd10108895f84d8232f6c34019
range:
  branch: origin/master
  end: 5a94df7e914b90c185fb573b6e7a6b3fb5f2460b
  mode: first-parent
  limit: 4
  skip_untouched: main-sources
scope:
  rule: explicit
  classes:
    - com.networknt.schema.PrefixItemsValidator
    - com.networknt.schema.PropertiesValidator
    - com.networknt.schema.TypeValidator
    - com.networknt.schema.CollectorContext
    - com.networknt.schema.SpecVersionDetector
dev:
  stable_runs: 1
  groups:
    - {name: dev, denominator: scope-targeted}
model:
  provider: openrouter
  name: openai/gpt-oss-120b
  env_file: null
  env_key: BATCH_COMPARISON_NO_MODEL_KEY
generators:
  - name: ouedraogo
    method: ouedraogo
    impl: native
    params: {prompts: [ZSL, FSL, CoT, ToT, GToT]}
    rounds: {max: 1, until_dev_matched: false}
screen: {stable_runs: 1, dedup: exact-body, matching: none}
survival:
  flaky_rerun: 0
  environment_errors: {patterns: [], max_reruns: 0, backoff_seconds: 0}
coverage: {changed_line: true, at_anchor: true, groups: alive-entering}
runner:
  docker:
    image: survival-java:25
    m2_volume: batch-baseline-comparison-m2
  reuse: {checkouts: null, classpath_cache: null}
  workers: {build: 1, survive: 1, cover: 1}
  timeouts: {build: 600, compile: 300, test_class: 120}
  jvm: {launcher: 1g, javac: 512m, jacoco: 1g}
```

Use Python 3.12 and pin/record the actual image ID; `survival-java:25` is the
existing image used by #31's fixture, not a public image assumed available on
every machine. Keep the same image, dependency contents, CPU/memory allocation
and JVM settings for both runs. Leave `SURVIVAL_JVM_JACOCO` unset. Use independent
copies of an equivalently warmed scratch Maven volume for timing, recording
their names and cache preparation outside the timer.

Run order for each destination, after Part 2 is authorized:

```sh
survival run /tmp/batch-baseline-comparison/repo/experiments/legacy --until scope --no-panel
survival import /tmp/batch-baseline-comparison/repo/experiments/legacy --from "$HOME/_projects/experiment-projects/json-schema-validator/experiments/commit-survival" --what generated
survival run /tmp/batch-baseline-comparison/repo/experiments/legacy --stage screen --no-panel
survival run /tmp/batch-baseline-comparison/repo/experiments/legacy --stage control --no-panel
survival run /tmp/batch-baseline-comparison/repo/experiments/legacy --stage survive --no-panel
survival run /tmp/batch-baseline-comparison/repo/experiments/legacy --stage cover --no-panel
survival run /tmp/batch-baseline-comparison/repo/experiments/legacy --stage report --no-panel
```

Repeat with the batch destination/configuration. Preparation and scope come
**before** import because they invalidate later stage state. Import copies all
generator files into scratch and marks generation complete; subsequent explicit
stage commands never invoke `generate`. Keep the dedicated model key absent.
Do not import `dev-results`, survival, or coverage: both paths must actually
measure. Compare frozen IDs and source/resource hashes before interpreting
results; if fresh screening differs between copies, resolve that input mismatch
before attributing a difference to execution mode.

This is intended to finish in under one hour **per mode**, including fresh
prepare/scope/import/screen/control/survive/cover/report, but that limit is not
measured yet. The old source summary records approximately 84 s for developer
baseline, 15 s for anchor control, and 9 s for Ouédraogo screening; the comparison
has only two non-anchor measured commits. Those are historical estimates, not
current timings. Record a monotonic wall-clock total and separate preparation,
screening, control, survival, aggregation and report timings. The primary
measurement cost comparison is control + survive + cover; report whole-run time
too. Run modes sequentially with the same warm-cache policy. A one-hour harness
deadline must call `survival stop` on the scratch experiment and retain evidence;
report an overrun rather than presenting an aborted run as a completed comparison.

Comparison output must enumerate:

1. Every `(index, group, frozen method ID)` cumulative outcome and first failure,
   normalizing only legacy fail/error/timeout to fail. Compare row zero, carries,
   raw available observations and chronological dead sets separately. A skipped
   method has no execution to compare; parallel speculative extra executions
   are not new losses. Use one worker for the primary timing/parity slice and
   fixture tests for parallel behavior.
2. Every `(index, group, file, line)` coverage value and executable universe,
   including t and alias-resolved predecessor maps. Compare all/scope/other
   changed-line counts, percentages, losses and pre-image exposure columns;
   retain missing-versus-zero distinctions. Log each mismatch with source, raw
   observations, run diagnostics, and a concrete cause. Unknown cause remains
   unresolved, not an inferred flaky result.
3. Total and stage wall times, source/image/tool identities, fallback/timeout
   counts, restoration counts, final-summary availability, and stop/resume reuse.
   Semantic equality is expected where mechanisms permit it; literal byte
   equality is inappropriate for timestamps, durations, mode metadata and
   different job layouts. Any substantive difference must remain visible.

### Developer-only policy exercise

Create a third sibling experiment `experiments/policies` with the common config
and batch mode. Point it at the **completed scratch batch baseline**, not at
itself. Add:

```yaml
policies:
  baseline: experiments/batch
  regenerate_developer: false
  groups: [dev]
  releases: [batch-comparison-release]
  threshold: 0.5
  max_threshold_triggers: 1
  set:
    - {name: release-reset, trigger: release, action: reset, developer: regenerate}
    - {name: half-reset, trigger: threshold, action: reset, developer: regenerate}
    - {name: half-fill, trigger: threshold, action: fill, developer: keep}
```

Then run `survival policy plan` and `survival policy run --until pools` against
that policy experiment. There is no policy CLI `--group` switch: selection is
`policies.groups`. A release developer pool at index 2 must be built even though
the global default is false. The short slice need not cross the 50% developer
threshold; fixture tests must cover the threshold and keep branches explicitly.
This manual check stops at pools as requested. Segment opening and subsequent
execution belong in the end-to-end fixture, not in an unrequested live policy
extension. Generator pools are excluded, so this exercise makes no model calls.

## Conflicts, corrections, and footprint constraints

| Request/context or tempting implementation | Current-code evidence / conflict | Proposed resolution |
|---|---|---|
| The supplied target YAML should validate unchanged. | `config.py` has no `survival.execution`; `policy/config.py` rejects `developer` in set entries. `flaky_rerun` defaults to 1. The example is a fragment: policy `baseline`, `groups`, and normal experiment fields remain required. | Add keys, choose documented ignore semantics for batch flaky reruns, and test the exact fragment merged into a complete config. |
| One compile and one JVM per index. | Policy has helper compilation, one compile per source root, selected-file fallbacks, eight-manifest-group chunks, and failed-chunk per-file execution. | Interpret as one shared **test execution job** per index with those explicit existing exceptions; do not promise one total OS Java process. |
| Baseline and policy coverage are interchangeable. | Baseline uses alive entrants and all production lines; policy uses post-transition survivors and scope lines. | Preserve baseline measurement population/output scope; import its coverage before regeneration and project policy populations afterwards. |
| Per-test hits alone preserve every existing output. | Line booleans cannot reproduce full JaCoCo XML counters; no-test policy batches lack a universe; teardown coverage can differ. | Extend batch evidence/analyzer output and hold classes until ordered projection. Disclose and explain measured differences instead of inventing equivalence. |
| No re-execution of imported baseline methods. | Segment 1 and `none` already import, but later carried methods go through `Runs.run_index()` with no baseline seeding. | Add current-schema, identity-checked per-index reuse for batch baselines; preserve legacy lineless semantics and fallback remeasurement rules. |
| Remove the two pool stable-run assignments. | Developer worker also hard-codes `repeats=3`; shared `screen_round()` always names the count `stable_3_of_3`. | Fix all three run-count sites and the shared funnel writer/readers, testing 1 and 3 runs and legacy funnels. |
| Developer regeneration has not completed end to end since #20–#29. | #31's committed `docs/footprint-verification.md` records a real synthetic baseline/policy fixture with developer pool and reset/fill execution. This is not evidence for mixed per-policy settings or the new real-project batch baseline. | Extend that fixture rather than claiming there is no existing evidence; add the required new paths and report their actual coverage. |
| Plan should ignore terminal regeneration points without later evaluation. | `plan.build_plan()` accepts any evaluable index at/after the request, including the last one; existing docs say points without a later evaluable commit do not create pools. | Reconcile planner/docs in Part 2: explicitly record terminal candidates without scheduling unnecessary pools if that documented rule is retained. The comparison release has a later measured index, so it does not depend on this discrepancy. |
| Run policy commands "on" the batch comparison experiment. | `Baseline.guard()` rejects policy output overlapping its baseline. | Use the sibling policy workspace pointing to the batch baseline; do not weaken the read-only guard. |
| Finished legacy fingerprints must remain valid. | `Configuration.as_written()` protects stage hashes, but raw `run_fingerprints()`, `dev_results.identity()`, and policy config expansion also consume resolved dictionaries. Runner provenance includes files this feature will change. | Normalize omitted/default legacy mode at every identity boundary; preserve known historical provenance only where semantics are unchanged. Add pinned pre-change hash/import/progress tests, not a blanket compatibility exception. |
| Delete checkouts immediately after batch worker return. | #31 releases only after the final consumer validates outputs; baseline currently holds planned coverage indices through `cover`. | Keep shared evidence/classes until all exact-entry coverage/XML and group projections validate. Then release once; pure `cover` must not restore builds in an uninterrupted run. Preserve anchor/pool finalization rules. |
| Add a new batch scratch folder without lifecycle changes. | #31 cleanup dispatch knows `phase-A`, `phase-B`, and `policy-survive`; ownership and failure retention guard deletion. | Register baseline batch scratch, use shared worker ownership, retain failed/interrupted chunks, honor `--keep-work`, and retain original build logs. Test zero uninterrupted restorations, compressed evidence, and resumed partial projections. |

Part 2 should be delivered in the requested three implementation commits
(batch baseline; per-policy developer regeneration; configured pool screening),
each with relevant tests. Documentation updates cover README, regeneration
policies, result formats and tool settings. Run the small comparison and
developer-only pool exercise only after Part 1 confirmation. Preserve existing
legacy-layout tests, add real end-to-end fixture evidence, then open one PR and
leave it unmerged. Part 1 ends with this document committed and pushed; it does
not create that implementation PR.
