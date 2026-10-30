# Regeneration policies

Policies run after a completed baseline; the original prepare–report stages and
artifacts remain unchanged. Add a `policies` section to a new experiment's
`experiment.yaml` (the ordinary experiment settings are still required). Paths in
`baseline` and `baseline_config` resolve relative to `project.repo`.

```yaml
policies:
  baseline: experiments/commit-survival
  baseline_config: null
  regenerate_developer: false   # default true for compatibility
  groups: [dev-scope, ktester, ouedraogo]
  releases: ["1.5.0", "2.0.0"]
  threshold: 0.5
  max_threshold_triggers: 1
  generator_overrides:
    ktester: {params: {fix_tries: 3}}
  concurrency: {ktester: 3, ouedraogo: 3, dev_selection: 1}
  fill_seed: 20261029
  set:
    - {name: release-reset, trigger: release, action: reset}
    - {name: half-reset, trigger: threshold, action: reset}
    - {name: half-fill, trigger: threshold, action: fill}
```

```sh
survival policy plan experiments/regen-policies
survival policy status experiments/regen-policies
survival policy status experiments/regen-policies --watch
# Run only when ready to generate and measure:
survival policy run experiments/regen-policies --background --until pools
survival policy run experiments/regen-policies --background --until report
```

Planning never builds or calls a model. A tag maps to the first indexed commit
containing that tag by Git ancestry, including tags on merged side branches.
Tags already contained in the baseline anchor are outside the observation window
and do not create a checkpoint.
Thresholds are detected from cumulative baseline losses with an inclusive
comparison. With multiple threshold triggers, the remaining baseline population
at the preceding trigger is the next *planning* segment denominator; generated
outcomes never change the precomputed schedule. A zero denominator cannot trigger.
Points move forward past skipped/build-failed commits. Points outside the range,
or without a later evaluable commit, are recorded without creating a pool.
Coincident triggers share one pool and one segment transition per policy/group.

`baseline_config` is used only when the baseline has no configuration. Missing
commit paths are reconstructed in the new experiment. A missing skip list is
accepted only when every selected baseline commit/group has raw evidence; a
selection-only project-wide developer group needs no raw records. `raw-derived`
is checked before `raw`. The baseline is never used as a checkout or output tree;
overlapping output directories and symlinks into it are rejected. Policy
containers also mount the baseline read-only, including when its parent
repository is mounted writable.

Each pool has an isolated checkout at its regeneration commit, fresh source
inventory, the baseline scope, and its own generation/screening evidence beneath
`results/policy/pools/<group>/<index>/`. Missing scope classes are omitted without
substituting other classes. KTester rebuilds its index and focal-method dataset;
Ouédraogo rebuilds class prompts and dependency signatures. Baseline settings plus
validated overrides drive generation. Screening always uses three runs and
exact-body deduplication. Matching targets use the Dev-scope pool at the same
point when available, otherwise the Dev-scope segment denominator in force for
the requesting policy (the maximum if a pool serves policies with different
targets). For `until_dev_matched` after multiple Dev fill checkpoints, a same-index
Dev pool is required so the target is known before the commit loop. Developer
pools are built before dependent generator pools, with one shared concurrency
limit for developer selection.

With `policies.regenerate_developer: false`, only LLM groups regenerate. New
plans contain no developer points or threshold triggers. Saved plans retain their
original bytes and hash; execution filters their developer points and never runs
`dev-full` selection or developer pools. Existing developer pool artifacts remain
on disk and are ignored. Matching uses the fixed scoped developer denominator at
t. Every policy's `dev-scope` curve is the baseline no-regeneration reference;
tables and coverage outputs label it accordingly. Its coverage comes from the
baseline when available and is omitted when unavailable.

The default `true` retains developer regeneration. In that mode developer
selection runs the full suite with JUnit Platform and Vintage, then screens the
selected population for strict 3/3 passes.

Policy survival and coverage share one execution pass. Between successive
regeneration points, commit indices run concurrently with
`runner.workers.survive` workers. The coordinator aggregates those measurements
in commit order before deciding fill membership at the next transition. Each
evaluable index submits one worker job containing all non-importable methods
needed across policies. The worker compiles each source root (snapshot) once,
with its own sourcepath and class output directory. Different regeneration points
can therefore contain different versions of the same class without duplicate-class
errors or access to another snapshot's helpers. If a root batch fails, its files
fall back to separate compilation with their helpers; the failed whole-root
attempt is reused rather than repeated for each file. Screening
validation records exclude rejected test files before snapshot inventory or
compilation, and so do generator scratch subtrees beneath a kept test's source
root (`temp/`: KTester keeps every repair attempt under `<test_classes>/temp/`,
hundreds of files per pool, some unparsable). The pool snapshot copies every
sibling of a kept test, so without this exclusion the whole-root compile fails
at every index and an unparsable attempt can abort the per-file helper
inventory. Other unscreened siblings (helpers, providers, package
subdirectories) remain available to javac and `SourceInventory` as before.
`results/policy/excluded-sources.json` counts kept, rejected and scratch sources
per pool. Scratch copies leave pool sources and stored execution hashes intact.

All compiled methods execute in one JVM through `PerTestCoverage`, with isolated
class loaders for distinct snapshots. Each snapshot supplies its classpath and
filesystem resources. The helper dumps and resets JaCoCo after each test;
parameterized invocations are unioned by `class#method`. Only scope source-file
coordinates from `population/scope.json` are persisted for policy survival.
The JaCoCo agent and CLI version follow the runner JDK (0.8.14 on JDK 22+, including
JDK 25), even when production classes were compiled with `--release 17`.

The new policy execution path deliberately uses only `pass`, `fail`, `absent`,
and `compile-fail`. Any non-successful observed JUnit result is `fail`; missing
observations are `absent`. There is no fail/error classification, flaky rerun,
or environment-error retry. Non-pass methods retain their failure message,
truncated to 4,000 characters. JUnit messages use `Throwable.toString()` with the
exception class name first, preserving the information needed for later
Assertion-family versus other-exception classification. This PR does not perform
that classification. Compilation failures retain compiler diagnostics.

If the batch test JVM exits unsuccessfully or times out, the worker reruns that
index through the existing per-file survival path, with both retry mechanisms
disabled. It records survival, marks coverage missing, and continues. The
per-policy `coverage-missing.csv` lists affected indices/groups, including failures
at indices with no changed executable scope lines. Completed fallback records are
reused; resuming does not repeatedly retry missing coverage.

Segment 1 imports baseline raw evidence. At a regeneration commit the old segment
is evaluated first, then a second `regenerate` row opens the new segment. Reset
uses the entire pool. LLM fill replaces losses within a focal method (KTester) or
target class (Ouédraogo), up to the number lost, using a stable seeded sample.
Developer fill retains survivor sources and adds every pool `class#method` absent
from the survivors. `segments.json` records members, source hashes, replacement
units, and carried/new composition.

Shared survival runs are stored by execution hash and commit index. The execution
hash includes the test file, its frozen Java source tree, and resource/suite
contents using relative filenames; `file_hash` separately records the test source
content. Identical snapshots stored at different paths share runs, while changed
helpers or resources cannot overwrite a carried snapshot. A run contains the
union of methods needed from that snapshot across all policies; policy
rows select from that record. Baseline source records are also imported for
carried tests. Skips and build failures preserve the current alive set. The cover step performs no Java execution. It computes the union of alive policy tests after each commit's transitions, and counts
covered changed executable scope lines using the baseline executable universe.
Totals sum line/commit pairs, rather than deduplicating a line across history.
Container teardown probes are reset separately and excluded from per-test hits,
so one class's cleanup cannot contribute coverage to the next class's tests.
The `none` policy imports baseline survival and coverage unchanged. Imported
baseline raw and pool screening observations are reused without execution. When
such evidence has no per-test line hits, coverage of a regenerated composition
that needs those hits is unavailable, and is listed in `coverage-missing.csv`;
missing hits are never treated as zero. Before a group's first regeneration,
its complete baseline coverage is used directly. Totals retain `executable` and
`groups` and add `group_executable` denominators for available observations,
`reference_groups`, and `missing_indices`. The report uses those denominators
so partially measured coverage is not presented as a complete total.

`state/policy-*.json` records configuration fingerprints and completion for pools,
shared runs, coverage jobs, and stages. Changed inputs require `--force`; completed
work is otherwise reused. `regenerate_developer` is excluded from the digest;
changing it filters saved plans without invalidating any pool or measured-source
identity. Aggregate survival/coverage/report steps track their execution revision
separately. Coverage schema version 2 records the isolated snapshot compilation
semantics; version 1 cross-root measurements must be upgraded as well. Completed
measured run records without the current coverage schema are
recomputed automatically, without `--force`, in every experiment. Import and
skip/build-failure records remain reusable; no pool is regenerated for this
upgrade. New completed records (including explicit coverage-missing fallbacks)
are reused on subsequent resumes. Force archives policy artifacts under
`work/policy-history/` before rebuilding. Foreground and background policy runs
share the experiment's exclusive lock and run lifecycle with ordinary stages.
`survival stop <exp>` stops the coordinator and its nested pool workers/containers,
including cleanup after the coordinator dies. Worker ownership is recorded at the
parent experiment; background startup publishes that identity before releasing
the child. Policy evidence uses schema version 3; earlier policy caches require
`--force` to rebuild snapshot dependencies and isolate container teardown coverage. Status reports
interrupted steps when their process is no longer alive.

Policy runs print each step's name, completed/total items, elapsed time, and
estimated finish, with a heartbeat every five seconds. Pool totals count completed
pools with saved evidence; cached pools count toward completion but not the current
attempt's rate. An estimate stays unknown until fresh work completes. Other steps
count their output artifacts. `policy status --watch` refreshes the same metrics
every five seconds until the run stops; Ctrl-C exits the watcher.

The first pool failure stops new pool submissions, including the generator wave.
Already-running pools finish under the run lock before the command exits non-zero.
The failing step immediately saves its traceback in `state/policy-<step>-error.txt`
and prints that path. Completed pools remain reusable on retry. Developer source
snapshots already inside their destination pool are used in place; snapshots from
other workspaces are still copied.

Outputs under `results/policy/` include the plan, segment compositions, survival
and first failures, coverage per commit and totals, per-policy event tables,
`summary.csv`, `summary.md`, `consistency.md`, and a multi-panel
`survival-by-commit.svg`. `none/survival.csv` is byte-for-byte identical to the
baseline. Summary counts of tests entering after t count segment entries; a reset
can introduce a new source version of an existing developer test ID.

The checked-in [JSV plan](../examples/regen-policies/json-schema-validator/plan.md)
and [ta4j plan](../examples/regen-policies/ta4j/plan.md) were generated without
running pools or policy measurements. JSV thresholds are 10/10/4, releases 98/156;
ta4j thresholds are 21/21, releases 153→155 and 426. All five original JSV scope
classes are absent at index 156, so the fixed-scope pools there are empty.

## Selection-only developer groups in the baseline

Set `frozen: false` on the project-wide developer group:

```yaml
dev:
  groups:
    - {name: dev, denominator: project, frozen: false}
    - {name: dev-scope, denominator: coverage-scoped, derive_from: dev}
```

The default is `true`. Selection-only groups still drive Maven baseline and
per-method scope selection at t. This setting leaves prepare, scope, generate,
and screen hashes unchanged. Later stages filter old frozen-denominator artifacts
at read time, so screen need not run again. A scoped child of a selection-only
parent executes its selected methods directly in control, survive, and cover.
The whole frozen developer test tree is still compiled, including helpers loaded
only by reflection, method-provider strings, or service loading. A sourcepath
alone would not compile those helpers. It keeps its existing
selection and denominator, and no longer needs parent raw records. Control,
survival, coverage, and report outputs omit the selection-only group. Imported
developer observations use their recorded raw/raw-derived provenance even after
the parent becomes selection-only. New imports record the effective destination
folder. Measurement stages exclude selection-only imports before validating or
reading raw evidence, while scope selection retains access to imported baseline
and denominator data.

The four local experiment configuration edits for this rollout are recorded in
[resume-configs.patch](../examples/regen-policies/resume-configs.patch). These
external experiments are not part of this repository. The patch records only the
new options; their survive and cover worker limits remain 4. No experiments were
run to validate this change.
