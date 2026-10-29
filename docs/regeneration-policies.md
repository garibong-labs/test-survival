# Regeneration policies

Policies run after a completed baseline; the original prepare–report stages and
artifacts remain unchanged. Add a `policies` section to a new experiment's
`experiment.yaml` (the ordinary experiment settings are still required). Paths in
`baseline` and `baseline_config` resolve relative to `project.repo`.

```yaml
policies:
  baseline: experiments/commit-survival
  baseline_config: null
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
# Run only when ready to generate and measure:
survival policy run experiments/regen-policies --background --until pools
survival policy run experiments/regen-policies --background --until report
```

Planning never builds or calls a model. A tag maps to the first indexed commit
containing that tag by Git ancestry, including tags on merged side branches.
Thresholds are detected from cumulative baseline losses with an inclusive
comparison. With multiple threshold triggers, the remaining baseline population
at the preceding trigger is the next *planning* segment denominator; generated
outcomes never change the precomputed schedule. A zero denominator cannot trigger.
Points move forward past skipped/build-failed commits. Points outside the range,
or without a later evaluable commit, are recorded without creating a pool.
Coincident triggers share one pool and one segment transition per policy/group.

`baseline_config` is used only when the baseline has no configuration. Missing
commit paths are reconstructed in the new experiment. A missing skip list is
accepted only when every baseline commit/group has raw evidence. `raw-derived`
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

Developer selection runs the full suite in one JVM, with JUnit Platform and the
Vintage engine. `PerTestCoverage` dumps and resets JaCoCo data after each test;
parameterized invocations are unioned by `class#method`. It also analyzes those
dumps within that JVM, avoiding a report JVM per test. The selected population is
then screened for strict 3/3 passes. Coverage preserves resources and dependency
source roots from each test's source snapshot. Classloader isolation allows
multiple versions of a generated/developer test class to coexist in a coverage
job without launching one JVM per method. Different source snapshots with
filesystem-relative test resources are separate suite runs, each using one JVM.

Segment 1 imports baseline raw evidence. At a regeneration commit the old segment
is evaluated first, then a second `regenerate` row opens the new segment. Reset
uses the entire pool. LLM fill replaces losses within a focal method (KTester) or
target class (Ouédraogo), up to the number lost, using a stable seeded sample.
Developer fill retains survivor sources and adds every pool `class#method` absent
from the survivors. `segments.json` records members, source hashes, replacement
units, and carried/new composition.

Shared survival runs are stored by source content hash and commit index. A run
contains the union of methods needed from that source across all policies; policy
rows select from that record. Baseline source records are also imported for
carried tests. Skips and build failures preserve the current alive set. Coverage
uses the union of alive policy tests after each commit's transitions, and counts
covered changed executable scope lines using the baseline executable universe.
Totals sum line/commit pairs, rather than deduplicating a line across history.
The `none` policy imports baseline survival and coverage unchanged.

`state/policy-*.json` records configuration fingerprints and completion for pools,
shared runs, coverage jobs, and stages. Changed inputs require `--force`; completed
work is otherwise reused. Force archives policy artifacts under
`work/policy-history/` before rebuilding. Foreground and background policy runs
share the experiment's exclusive lock with ordinary stages. Status reports
interrupted steps when their process is no longer alive.

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
