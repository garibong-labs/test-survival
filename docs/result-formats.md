# Result formats

Runner setup resolves missing jars with `org.apache.maven.plugins:maven-dependency-plugin:3.6.1:get` and `-U`, so retries check for missing releases even after Maven has cached a failure.

For each jar, a failed Maven command is retried up to three times when its log contains `429` or `Could not transfer`. Each retry waits 60 seconds. Other command failures and checksum mismatches stop setup. Jars are resolved sequentially before parallel workers start.

Each attempt keeps a separate `results/runner-dependency-<group>.<jar-stem>-<attempt>.txt` log. The jar stem includes the version and classifier, so their attempt histories remain separate. The matching `.json` file records the coordinate, command, exit status, timeout, duration, log path, and expected and downloaded SHA-256 values for each attempt. Existing attempt logs are retained across automatic retries and later setup invocations.

A cached jar with the wrong checksum is removed and fetched again. The attempt records its rejected checksum. Downloaded jars must still match the pinned checksum before they are copied into the experiment.

CSV uses UTF-8, a header row, and LF line endings. JSON uses UTF-8 and two-space indentation. Paths in method records are experiment-relative. Default stable runs are three; configuring a different count changes the `runN` columns accordingly.

## Experiment files

Headers below follow the archived experiment and are checked by `tests/test_formats.py`.

`results/commits.csv`: `index,sha,date,subject,changes-src-main,changes-pom`. Index 1 is the first commit after `t`; index 0 is `t` itself and appears only in `survival.csv`. `date` is the committer date in ISO 8601 with offset. `changes-src-main` and `changes-pom` are `yes`/`no`.

`results/anchor.json`: keys `tag`, `sha`, `origin_master` (the resolved range end sha), `n` (commit count), `started`, `date` (committer date of `t`), `deviations` (list of strings), `key_check` (`KEY_OK (value never logged)` or `KEY_MISSING`), plus `ktester_base` (the KTester tool commit) when a ktester group exists.

`dev.unmapped` defaults to `error`, which stops preparation when an executed baseline case cannot map to exactly one source method. With `exclude`, preparation continues and excludes those cases (including unmapped skipped cases) from every Dev denominator. Mapped cases retain their existing stability rules. `dev/dev-unmapped-excluded.csv` contains one row per distinct `(class, name)`, with `runs_seen` counting distinct baseline runs and `reason` equal to `no-source-method` or `ambiguous-source-method`. `results/baseline-unmapped.json` retains per-run observations. `results/anchor.json` and generation funnels record the distinct-case count as `dev_unmapped_excluded`; anchor deviations, funnel notes, and report notes disclose the exclusion. `results/final-summary.json` also includes the count and notes.

`results/builds/<index>.json`: `index`, `sha`, `build` (`ok`|`fail`), `attempts` (list of `{command, cwd, exit_code, timeout, seconds, log}`). Attempt order: offline Maven first (`mvn -q -o -DskipTests compile`), then online if offline fails.

`dev/dev-methods-all.csv`: `class,method,kind,stable,run1,run2,run3`. `kind` is `plain`, `parameterized`, `test-factory`, or `test-template`. `stable` is `yes` when every run passed.

Both `dev-baseline` and `dev-results` imports include `dev/dev-unmapped-excluded.csv` in `state/import.json` file checksums when the source has it, remove stale destination audits when absent, and immediately refresh the anchor count and deviation note. Imported audit bytes are verified before baseline reuse. `dev-results` compatibility records store the resolved policy as `measurement_settings.unmapped`; an omitted field in an older manifest means `error`.

Batch `dev-results` compatibility additionally stores `resolved_scope`: sorted,
distinct `{class, file}` pairs from `population/scope.json`. Both experiments
must have resolved scope before importing. This proof is checked before copying,
on subsequent reuse, and against any newly selected scope before retaining
imported denominators. Batch manifests missing it require reimport; legacy
compatibility records and layouts remain unchanged.

`dev/dev-denominator.csv`: `class,method,kind`.

`population/class-survey.csv`: `class,file,m,has_test,d,c` where `m` is the focal method count, `has_test` `yes`/`no`, `d` the dedicated-test method count, `c` the number of commits in range that change the class.

`population/ktester-population.csv`: `id,class,method-name,test-class,test-path`. `method-name` is the signature with parameter types, for example `from(ValidationContext, String, URI, JsonNode, JsonSchema, boolean)`.

`results/scope-selection.json`: `classes`, `m`, `developer_denominator`, `eligible_classes`, `mandatory_stop`, `stop_reason`, `model_calls`, `max_dev_scope`, `skipped_classes`, `per_test_coverage_seconds`. Each skipped class retains its survey fields, `would_be_dev_scope`, and `reason: max-dev-scope`; `population/scope.md` lists the same skips.

`results/llm-<group>-denominator.csv`: `file,class,method,kind,body_hash,round,technique`. `file` is relative to the experiment folder. `technique` is the prompt type for Ouédraogo groups (`ZSL`, `FSL`, `CoT`, `ToT`, `GToT`) and `KTester` for the KTester group.

`results/frozen-denominators.json`: object keyed by group; each value a list of `{class, method, kind, file, id}` with `id` = `<file>|<class>|<method>`. `results/denominators.json`: object keyed by group with integer counts.

`results/raw/<index>-<group>.json`: `index`, `sha`, `group`, `phase` (`A`), `status`, `input_sha256`, `runner_sha256` (object of file name to sha of the runner components used), `files` (list of `{file, compile: {command, cwd, exit_code, timeout, seconds, log}, runs: [same shape], methods: [...]}`), `methods` (flat list of `{class, method, kind, file, id, state, message, flaky, initial_state, selector, baseline_states}`), `requested_ids`, `whole_frozen_suite_compile` (Dev only; `{command, cwd, exit_code, timeout, seconds, log, source_count, diagnostics}`), `seconds`. A skipped commit's record is `{index, sha, group, phase: "A", status: "skipped-untouched"}` only.

Parallel survival jobs request a superset of methods alive entering the index,
omitting only deaths confirmed by earlier completed jobs at dispatch time. `methods` still
contains the full frozen denominator, with previously lost methods recorded as
`state: "dead"`, `message: "not executed; failed at index N"`, and `flaky: false`.
These entries have no compile/run evidence; `requested_ids` lists only requested
methods. `input_sha256` continues to hash the full frozen list. Cumulative
aggregation ignores dead entries and extra executions outside the exact
alive-entry set, so they create no new first-failure rows. `N` is the earliest
failure known at dispatch; `first-failures.csv` retains the exact chronological
first failure even when jobs finish out of order.

`results/survival.csv`: `index,sha,date,subject,evaluated,` then for every group in column order (Dev groups in `dev.groups` order, then generators in configuration order): `<group>_alive,<group>_lost`. The same column order applies to every table. Row 0 is `t` with subject `t = <anchor>`. `evaluated` is `yes`, or `no` for build-failed and skipped commits (which carry the previous row's counts).

`results/first-failures.csv`: `index,sha,group,id,class,method,kind,flaky`. `kind` is one of `compile-fail`, `fail`, `error`, `timeout`, `absent`. One row per method at the commit where it was first lost.

`results/changes/<index>.json`: `index`, `sha`, `parent`, `pre_image` (object file -> list of executable changed line numbers at `c-1`), `post_image` (same at `c`), `diff` (the `git diff -U0` text). Written for every commit with `changes-src-main = yes`.

`results/coverage/<index>-<group>.xml` is the JaCoCo XML report. `results/coverage/<index>-<group>-lines.csv`: `file,line,covered` with `covered` `yes`/`no` for every executable line of the module's production sources. `results/raw-coverage/<index>-<group>.json` mirrors the raw survival record shape for the coverage run.

`results/coverage-commits.csv`: single column `index`, every index at which coverage was measured (production-changing commits and their predecessors, plus 0, excluding skipped indices; see the predecessor aliases below).

`results/alive-entering.json`: object `index -> group -> list of ids` alive entering that index.

`results/changed-lines.csv`: `index,sha,date,subject,` then for each of the three partitions `all`, `scope`, `other`: `<p>_executable`, then per group `<p>_<group>_covered,<p>_<group>_percent`, then after all three partitions, per group `<group>_lost` and per group the exposure counts as produced by the hand-run `aggregate_coverage.py`. Port the exact column list from `~/_projects/experiment-projects/json-schema-validator/experiments/commit-survival-run2/tools/aggregate_coverage.py`; the header must match that run's `results/changed-lines.csv` byte for byte when the groups are the same.

Tables. Match the hand-run files exactly in structure, including title lines, column order, percent formatting with one decimal, and `n/a` for a zero denominator:

- `table1-survival-by-event.md`: `# Table 1. Survival by event`; columns `Index | Date | Subject | <group> ...`; rows only at indices where any group's count changes, plus row 0 and the last index; cells `N (P%)`.
- `table2-breaking-commits.md`: `# Table 2. Breaking commits`, line `Cells show methods lost and first-failure kinds.`; cells `N (compile-fail: a, error: b, fail: c)` with kinds in alphabetical order and omitted when zero.
- `table3-changed-line-coverage.md`: `# Table 3. Changed-line coverage`, the two explanatory sentences used by the hand-run file, then sections `## All files`, `## Scope files`, `## Other files`, each with columns `Index | Subject | Executable lines | <group> covered ... | <group> lost ...`.
- `table4-exposure-vs-break.md`: `# Table 4. Exposure versus break`, explanatory line, rows `Exposed / lost`, `Exposed / no loss`, `Unexposed / lost`, `Unexposed / no loss`, `Unavailable`.
- `coverage-t.md`: `# Coverage at t`, columns `Scope file | Executable lines | <group> ...`, one row per scope production file, cells `N (P%)`.
- `consistency.md`: the JSON block `{nonincreasing, index_zero_equals_denominators, raw_records, expected_raw_records, raw_count_matches, missing_records, build_fail_indices}`, the coverage sentence, and the `## <N>-group checks` table with rows `Fixed denominator`, `Final survivors`, `Survival records`, `Coverage records`, `Matching executable universes`.

Group display names in tables: `dev` → `Dev`, `ktester` → `KTester`, `ouedraogo` → `Ouédraogo`, `ouedraogo-adj` → `Ouédraogo (adjusted)`, `ouedraogo-adj2` → `Ouédraogo (adjusted 2)`. Put the mapping in configuration as an optional `display` key on each generator entry, defaulting to the name.

## Batch baseline results

With `survival.execution: batch`, the baseline keeps survival/failure/denominator
and report formats, with these explicit differences:

- `results/baseline-batches/<index>.json.gz` stores the shared observation grid,
  input and runner identities, coverage schema, scope executable `universe`,
  `compile` attempts, chunk `runs`, fallback `files`, and canonical methods.
  `<index>-measurement.json` retains the worker's latest measurement evidence.
- Group raw/control records add `execution: batch`, `batch_input_sha256`,
  `measurement_source`, `coverage_version`, `coverage_status`, `coverage_reason`,
  and ignored retry settings. Methods retain baseline IDs and add source,
  resource and execution hashes plus `lines: [[file, line], ...]`. Missing
  `lines` means unavailable; an empty list means no measured hits. Previously
  lost methods use `state: dead` and no new execution evidence.
- Batch outcomes are `pass`, `fail`, `absent`, `compile-fail`, with `flaky: false`.
  JVM-failure fallback disables both retry mechanisms. Remeasuring missing hits
  preserves consumed verdicts and records `remeasured_state` on disagreements.
  Repairs with `remeasured_state: absent` or `compile-fail` remain unavailable
  and are retried on resume; an empty line list alone does not make them usable.
  Policy imports and shared-run caches apply the same rule. They retain the
  recorded survival verdict while repairing coverage, including previously
  seeded records that incorrectly claimed `coverage_status: complete`.
- Local derived groups inherit their parent's observations, including when the
  parent is imported. Chained derivations preserve per-test lines and verdicts
  without executing the child independently; imported files remain unchanged.
- `results/coverage/<index>-<group>-lines.csv.gz` contains only executable scope
  source lines. Empty test groups still get the scope executable universe and
  uncovered rows. There is **no** `results/coverage/<index>-<group>.xml` (or gzip
  XML) in batch mode, and no retained per-member probes for later XML creation.
  Coverage-scoped developer selection at t remains its separate existing stage.
- `raw-coverage` records are projections of the original batch, not second runs.
  `phase: B` identifies the consumer, with exact alive-entry method membership.
  A failed coverage observation uses `status: coverage-missing` and no line CSV.
- `changed-lines.csv` keeps the legacy header. All `all_*` and `other_*` values
  are empty/unavailable; `scope_*` values are populated where measured. Exposure
  counts and table 4 use scope pre-images. Skipped predecessors use the existing
  `coverage-index-aliases.json`; build failures remain unavailable.
- Coverage aggregation and reports never execute Java tests. Index zero's hits
  come from batch control; other indices' hits come from survival. Ordered
  projection removes speculative executions outside the exact alive-entry set.
- Batch control and survival publish `results/policy/excluded-sources.json`,
  the per-tree counts of kept, rejected and scratch sources. Status, progress
  and completed-stage audits calculate exclusions without publishing evidence,
  even when the summary is absent. Audits wait for the resolved scope, anchor,
  frozen denominators and (for survival) commit sequence/path records.
- The final summary includes `execution: batch`, `coverage_scope: scope` and
  `ignored_settings`. Scope-only measurement does not itself mark a run partial;
  genuinely missing scope measurements do. `phase_seconds.cover` is aggregation
  time. All/other report sections explicitly say unavailable.

Legacy mode is the default and retains full-production line maps and XML. The
new mode does not migrate finished legacy experiments or accept their lineless
raw records as batch measurements. Per-index batch input identities include the
implementation, scope, selected sources/helpers/resources, runner and settings.
Status and resume retain the existing run lock and worker/container ownership;
failed or interrupted batch scratch is retained. Successful checkouts release
after durable group projections validate, before the non-executing cover stage.

## Affected-selection records

Fresh batch method rows add `selection_action: executed | carried`,
`last_executed_index`, and `last_executed_sha`. A carried row adds
`carried_from_index`, pointing directly to the actual observation, and retains
that observation's state and scope `lines`. Dead/not-selected inventory markers
and unevaluated commit markers are excluded from execution counts.

Affected actual executions add a `dependency_record` containing `version: 4`,
`selection: affected`, sorted module-relative production `files`, `unbounded`, and
`context`. Dependencies union runtime production class hits, compiled test-tree
references (stopping at production classes), and production simple names found as
Java identifiers in the selected/reached test sources. Nested/local/anonymous
classes map through classfile source metadata to source files. Unmapped project
classes or incomplete analysis are conservatively unbounded. Only dependency
file sets and existing scope lines persist; no full-production line coverage,
probe dumps, or XML are added by selection. Legacy/all rows without dependencies
remain readable observations and cannot authorize an affected carry.
Version 4 retains production class hits from pre-plan discovery (such as Vintage
parameter providers), setup from every ancestor test container, and lazy class
initialization in both production and frozen test-tree classes. Initializer hooks
run after JaCoCo instrumentation, preserve its probes and class IDs, and retain
class hits for later methods in the same snapshot. These hits are conservative:
an initializer may reuse a probe hit earlier in the current interval, so all
current production hits at initializer completion are retained. This can cause
extra executions; later test-body hits are not retained by this mechanism.
Incomplete initialization, unavailable hooks, a custom loader unable to see
the hook bridge, or a known class loaded without its expected code source marks
the dependency record unbounded. Scope lines still use
only the original per-test probe dump. No frozen class files are rewritten.
Prototype version-1/2/3 dependencies stay readable but require execution before
they can authorize another carry or satisfy an affected policy execution request.

`results/selection-context/<index>.json` records the commit SHA, coordinate-list
version, `known`, and the ordered resolved third-party classpath entries
`[group, artifact, version, classifier, type]`. Unknown contexts include a reason.
The method's stored context always belongs to its last actual execution.

Raw group projections add `selection_counts: {executed, carried}`. Baseline
`final-summary.json` adds `selection`, post-anchor `selection_totals` per group,
and separate `selection_control` counts. A selected compile-fail/absent method
counts as executed for scheduling; compile/run evidence still shows whether
its body actually ran. Repairs do not increase the logical method/index count.
Imported pre-selection batch observations without `selection_action` use the
same rule for summary totals, control counts, and local derived-group counts:
actual pass/failure observations count as executed, explicit actions are
preserved, and dead/not-selected markers are excluded. Summary totals include
only evaluable indices. Counting does not add tags to imported rows or rewrite
their evidence or provenance hashes.

Policy physical observations remain in `results/policy/runs/<execution_hash>/`.
Affected logical rows are in
`results/policy/<policy>/decisions/<group>/<segment>/<index>.json.gz`; coverage
consumes the active segment's logical row. Per-policy `selection-counts.json`
records index/group totals, with each method counted once across boundary phases
(as executed if any phase executed it). `selection-phases.json` retains affected
phase counts, and per-policy `final-summary.json` contains group totals. These
JSON additions leave all survival/failure/coverage table schemas unchanged.
For legacy baseline observations and seeded/cached physical policy observations,
an evaluable pass/failure row without `selection_action` counts as executed.
Explicit executed/carried tags
are preserved; unevaluated indices and dead/not-selected markers do not count.
Normalization happens only in policy accounting and copied observations and
supplies no dependency record or execution origin. The read-only baseline and
usable cached physical records are not rewritten to add these tags. Counts are
updated incrementally; only the current index retains method identities for
deduplication across evaluation and regeneration phases.

Compile evidence includes `strategy: remove-error-files-v1`, each attempt's
command, input files, log, time, and `removed` map of source files to diagnostics.
The final result preserves the complete removal map even when a retry succeeds.
Each attempt uses clean class output; removed sources cannot be rediscovered
through the scratch sourcepath. No frozen/imported source is changed.

Each instrumented JVM `runs` entry adds `memory`: `peak_rss_bytes` is Linux's
process resident-memory high-water mark; `heap_pool_peak_sum_bytes` and
`nonheap_pool_peak_sum_bytes` sum memory-pool peak usage (individual pool peaks
need not occur simultaneously). Missing shutdown evidence, including forced
termination, leaves memory unavailable rather than reporting a measured zero.
These are per-JVM peaks; they are not aggregate peaks of concurrent JVMs.

Finished artifacts keep their original fields and remain readable. New code does
not rewrite them to add dependencies or fix historical compilation verdicts.
Exact historical resume fingerprints are not promised. Selection decisions and
both executed and carried rows participate in current-run completeness/progress;
status stays read-only and stop/resume use the existing ownership protocol.

## Archives

`survival archive EXP_DIR [--dest DIR] [--include-work] [--prune] [--yes] [--dry-run]`
packages a stopped experiment without loading or rewriting `experiment.yaml`.
Baseline and policy runs currently share `state/run.lock`; archive holds that
lock and any existing `state/*.lock`, `state/policy/run.lock`, or
`results/policy/state/run.lock` throughout writing, verification and pruning.
Running run records and incomplete stop cleanup also block the command.
Dry runs open existing locks only and write nothing.
When `state/run-owner.json` identifies a policy-pool coordinator, archive first
holds the owner's existing locks and checks its run/stop state for the entire
operation, including dry runs and pruning. A different current run ID does not
bypass ownership. The owner must be an ancestor with an existing run lock;
invalid or unavailable owners cause an error instructing the user to archive
the owning experiment. No files are created or changed outside `EXP_DIR` to
guard an owner.
Every present ownership record must be a regular JSON object with an absolute
owner path. JSON `null`, other non-object values, and symlinked records (including
broken links) are invalid; only a missing file means no recorded owner.

The destination layout is:

```text
archive-index.csv
ta4j/
  ta4j__commit-survival__2026-11-06.zip
  ta4j__commit-survival__2026-11-06.manifest.json
```

Both existing archives and reserved `.partial`/manifest names are skipped,
using `-2`, `-3`, etc. Publication uses native exclusive rename on macOS/Linux,
so a competing file created during copying cannot be overwritten. Filesystems
without that operation fail safely before pruning. ZIP entries use fork-relative paths, for example
`experiments/commit-survival/results/survival.csv`. Extract into the fork root
to restore them. `ARCHIVE-MANIFEST.json` is the only metadata entry at ZIP root.
Empty directories are retained; symbolic links are stored as Unix ZIP symlinks
without following their targets (restore with an extractor supporting symlinks).
Special files such as sockets and FIFOs cause an error.

Default regenerable exclusions are directories named `checkouts`, `node_modules`,
`.venv`, `target`, `dist`, `__pycache__`, `.gradle`, `.pytest_cache`, `.pnpm-store`,
and `compile-cache`; the experiment's top-level `work/`; all `work/` directories
below `results/policy/pools/`; and `.DS_Store` files. `--include-work` removes
the two work exclusions only. The entire top-level `generated/` tree and each
`results/policy/pools/<group>/<index>/generated/` tree are protected from exclusions,
including cache-like names, because model output costs money. Pool group/index
directories are structural identities and are never excluded by their names.
Actual cache/work directories within a pool retain the ordinary exclusion rules.
Excluded directories are pruned before recursion during archive creation.

The embedded manifest fields are:

- `format_version` (currently 1), `tool_commit` (null when no tool Git checkout is
  available), `archived_utc` (UTC ISO 8601), `exp_dir` (resolved absolute path),
  `fork`, `remote_url` (origin, or null), `branch` (null for detached HEAD),
  `head_sha`, and `dirty` (Git's porcelain status, including untracked files).
- `stage_states`: the complete JSON records from regular, non-symlink files in
  `state/*.json`, keyed by filename. Metadata reads use directory descriptors and
  no-follow opens; symlinks remain ZIP links without embedding their targets in
  either manifest.
- `included_files`, `included_bytes` (uncompressed payload bytes), and
  `included_directories`; file counts include symlinks and their link-text bytes,
  and exclude the embedded manifest itself.
- `exclusion_rules`: `directories`, `files`, `work`, and `protected` rules used.
- `top_level_entries`: each entry's `included_files`, `included_bytes`,
  `included_directories`, `excluded_files`, `excluded_bytes`, and
  `excluded_directories` (number of directory roots skipped).
  Excluded file/byte counts are null when an excluded directory was not scanned;
  `excluded_counts` explains this convention. Dry runs traverse excluded trees
  solely to print exact totals; archive creation avoids that potentially costly
  traversal.

The ZIP uses DEFLATE and ZIP64, written once to a local temporary file beside
the fork. After closing it, `testzip()` verifies CRCs, and the entry count and
sum of uncompressed sizes are checked against the walk totals plus directory
entries and the embedded manifest. A chunked copy is flushed to `<name>.partial`
and renamed; SHA-256 of the final destination must match the local ZIP. The
sidecar contains the embedded manifest plus `sha256`, which cannot be embedded
in the ZIP it hashes. Verification through the Drive desktop filesystem does
not independently confirm that Google's servers have received the upload.

`archive-index.csv` is UTF-8 with this header:

```text
archived_utc,fork,exp_dir,zip_relative_path,files,bytes,sha256,branch,head_sha
```

`zip_relative_path` is relative to the destination root; `files` and `bytes`
match the included payload totals. Each verified copy appends one row under an
advisory index lock. If a valid existing index lacks a trailing line terminator,
a separator is inserted under that lock before appending the new row.
Sidecar/index write failures prevent pruning, as do CRC,
count, size, or SHA failures. A failed copy may leave a final ZIP for inspection;
it is never recorded as verified or overwritten on retry.

`--prune` requires the verified ZIP, sidecar and index, and a typed experiment
folder name unless `--yes`. Before deleting, the command checks that included
paths and their metadata and the Git-tracked path set have not changed.
Only entries inside `EXP_DIR` are deleted; tracked files, tracked submodule
directories, and every acquired lock file are retained. Lock parent directories
also remain: published lock inodes must never be unlinked, even after release,
since another process may already hold a descriptor to them.
Excluded scratch and nested Git repositories are
removed as ordinary directories. Traversal and deletion are relative to open
directory descriptors; child directories are opened with `O_DIRECTORY | O_NOFOLLOW`
and checked for identity changes. Directory-to-symlink swaps cannot redirect
deletion to the link target. Read-only parent directories are retried after
adding owner permissions via `fchmod` on their descriptors, and symlinks are
unlinked without following them. Marker creation uses the same root descriptor
as pruning. Reported
bytes freed are logical file bytes removed, not filesystem allocation changes.
Tracked names on macOS are matched using normalized spelling and filesystem
identity, preserving files even when Git and `scandir` use different Unicode forms.

`EXP_DIR/ARCHIVED.json` contains `destination` (absolute ZIP path), `sha256`,
`archived_utc`, and `retained_status_files` (experiment-relative paths to SHA-256
for regular, non-symlink `experiment.yaml` and `state/*.json` files retained after pruning).
The command refuses pruning if this marker is Git-tracked.
`survival status EXP_DIR`, including `--watch`, prints `archived to <path>`
without requiring the deleted configuration or stage state. It ignores the marker
when configuration/state files have been added or changed since pruning, so
ZIP restoration, initialization, and resumed baseline/policy runs show their
normal status. Unchanged tracked configuration/state and permanent lock files
do not suppress archived status. Older markers without `retained_status_files`
use an empty retained set. This check is read-only and does not modify run
state, stage hashes, or execution provenance.

## State and imports

`state/<stage>.json`: `status`, `config_hash`, `started`, `finished`, `notes`. Status is `pending`, `running`, `completed`, or `failed`; the CLI reports `stale` for completed hashes that differ. Group-only generation also records `group_hashes`.

`state/run.json`: `pid`, `log`, `status`, `started`, `finished` when finished. `state/run.lock` is the advisory process lock. `state/log.txt` contains timestamped stage summaries.

`state/import.json`: keyed by `generated` and/or `dev-baseline`; each entry contains `source`, `files` (relative path to SHA-256), `source_sha256`, and `imported`.

`generated/<group>/rounds.json`: `source: imported` and `source_path` for imported data, or `config_hash` for new generation; `rounds` contains `round`, `directory`, `funnel`, and optional `model_calls`/`source`; `stopped` records the stop reason. Imported directories each get an independent ordinal `round`; `archived_round` and `archived_retry` retain the original numbering, with legacy `roundN-retry` mapping to archived retry 1.

Funnel JSON records `group`, `round`, `files`, `compiled`, `methods`, `stable_runs`, `stable_passed`, `duplicates`, `kept_this_round`, `total_kept`, `developer_denominator`, `ratio_to_dev`, `per_prompt`. The stable count means methods passing every configured screening run. Readers accept old `stable_3_of_3` counts; new funnels explicitly state the run count. Developer pool funnels use the same two fields alongside `discovered`, `selected`, and `kept`. Reading old funnels does not rewrite their source files. Compile-failure records contain `file`, `category`, and `diagnostic`.

## Dashboard mappings

| Layout | Inputs | Commit index | Alive | Denominator | Coverage |
|---|---|---|---|---|---|
| unified | survival.csv, denominators.json, commits.csv, experiment.yaml when present | index | `<group>_alive` | denominators.json | sum `all_<group>_covered` / sum `all_executable` over measured rows |
| per-group (jsoup) | `<group>-survival.csv`, including pooled-survival.csv | index | survived | denominator | not measured when changed-lines.csv is absent |
| per-group (commons-codec) | dev-survival.csv, llm-survival.csv | index | alive_count | alive_count at index 0 | sum `all_<group>_covered` / sum `all_executable_changed_lines` |
| testpilot-ss | survival-by-commit.csv | n | dev_cum_subtests, llm_cum | respective index-0 counts | not measured when changed-lines.csv is absent |

The dashboard embeds project, language, anchor, commit count, denominators, per-group curves, and coverage totals as JSON. Zero-denominator groups are unavailable for percentage curves. Missing measurements remain unavailable.

## Report metadata

`results/survival-consistency.json`: `nonincreasing`, `index_zero_equals_denominators`, `raw_records`, `expected_raw_records`, `raw_count_matches`, `missing_records`, `build_fail_indices`. Missing-record entries identify `index` and `group`.

`results/coverage-consistency.json`: `coverage_indices`, `production_changes`, `identical_executable_line_sets` (indices), `missing` (index/group entries), `zero_executable_change_commits`, `unavailable_commits`. Report adds `totals`, keyed by all/scope/other with `executable`, group covered counts, and `methods_lost_in_listed_commits`; `baseline` maps scope file to `executable` and per-group covered counts.

`results/final-summary.json`: `status`, `finished`, `denominators`, `final_survivors`, `model_rounds` (group to saved round manifest), `phase_seconds` (completed stage elapsed times), `final_verification`, `survival_consistency`, `coverage_consistency`, `pushed`. Verification includes `nonincreasing`, `raw_count_matches`, `missing_survival_records`, and `missing_coverage_records`.

`results/figure1-survival-by-commit.svg` is the mandatory chart. The JDK renderer additionally writes `.png`; `figure-render.json` records its command result and `figure-render.log` contains diagnostics.

When a KTester dataset exists, `work/ktester/<group>/experiment-result.json` records `status`, `fork_commit`, `fork_branch`, `anchor`, `denominators`, `model_rounds`, `phase_seconds`, `verification`, `final_summary_sha256`, `report`, `author_code_modified`, and `pushed`. This record belongs to the new experiment, never to the author-tool checkout.

## Measurement and generation evidence

Command records use `command`, `cwd`, `exit_code`, `timeout`, `seconds`, and `log`. Build records also include `classpath` and `input_sha256`. Method retry records additionally include `retry_state` and `retry_message`. Coverage raw records use phase `B` and add `coverage_report`. Empty test groups still receive coverage reports describing every executable production line with zero coverage.

### Compilation reuse and execution history

Group workers (survival, coverage, control, and per-method coverage) reuse **successful**
compilations in `work/compile-cache/v1/`. Screening and import repair retain their existing
paths. The key includes the commit, working directory, ordered explicit sources,
sourcepath tree, ordered classpath contents (including production classes), JDK compiler
contents/version, relevant environment digest, heap option, and timeout. File timestamps
that affect javac source/class selection are included. Content hashes are memoized only
inside a worker and invalidated on device/inode/size/mtime/ctime changes.

Unsupported inputs (symlinks, implicit/wildcard classpaths, manifest `Class-Path`,
annotation processors/compiler plugins, external JVM/compiler/library options, or an unverifiable JDK) bypass
reuse and execute the original javac command. All compiler failures, timeouts, and
process/environment errors are executed again. No negative cache or cross-commit failure
propagation is implemented. A failed whole-suite compile still falls back to each file.
An empty developer group skips compilation but still produces its coverage line universe.

Each key has a cross-process lock. Complete artifacts are published by directory rename,
verified by content hashes before reuse, and copied into the job's private classes tree;
tests never receive the cache directory as their runtime classpath. Corrupt/incomplete
entries are misses. The compiler module participates in the existing runner provenance
hash, so older result records are readable but are not accepted as current measurements
when a worker is requested with changed provenance. Existing stage skip rules remain.

Compile command records add `cache`: `status` (`hit`, `miss`, or `bypass`), `event`,
optional `key`/`origin_event`/`reason`, and `elapsed_seconds` (lookup, locking, hashing,
compilation/restoration, and publication). On a hit, command `seconds` is **zero**, not
the original compilation's duration. Test runs and method/denominator fields are unchanged.
Whole-suite compile records are still referenced by multiple `files[].compile` fields:
deduplicate by `cache.event` (or command/log for older records) when totaling costs.

Legacy `results/compile-events/<unique-id>/` is independent of replaceable worker scratch and
stage state. `started.json` stores context, command, and start time; `finished.json`
stores finish time, `executed`, and the command result pointing to a durable `javac.txt`.
`interrupted.json` records an exception when possible; a start without a finish can mean
the process was killed. The first cacheable request in a worker also stores its JDK
identity probe as `toolchain.json`/`toolchain.log`. These events count compile requests,
not full-result cache hits: returning a cached group result does not start a worker or
create new compile events. They do not reconstruct older overwritten stage histories.

This cache does not claim an end-to-end speedup. It adds hashing, verification, copying,
and ledger I/O; failed compilations remain uncached. Validate wall time on a future
authorized run separately from reduced javac invocation counts.

Native model-call directories contain `request.json` (model, temperature, max_tokens, messages), `raw-response.json`, `response.md`, `usage.json`, and `run.json`. The latter records `timestamp`, `latency`, `status`, `http_status`, `finish_reason`, `provider`, `model_requested`, `model_reported`, `request_id`, `usage`, `error_type`, `prompt_sha256`, and `response_sha256`. No authorization headers or credentials are saved.


## Developer groups, skips, and environment retries

`results/dev-method-coverage-t.csv`: `class,method,scope_lines_covered,scope_files,total_lines_covered,seconds,status`. `scope_files` is a JSON array of scope file paths. The reference addendum calls this column `scope_files_touched`; comparisons accept that historical spelling. Historical per-method JSON, JaCoCo XML, and executable-line CSVs remain readable under `results/dev-method-coverage-t/<class>#<method>.*`. New runs write `results/dev-candidate-coverage-t.json` with candidate states and line hits, plus `results/dev-candidate-coverage-t-timing.json` with total seconds, test-JVM seconds, candidate count, and JVM count. The CSV `seconds` field is empty for batched observations; no per-method duration is invented.

`dev/<group>-excluded.csv`: `class,method,kind,status,scope_lines_covered,reason`. Reasons are `failed-alone`, `no-scope-line-covered`, or `not-scope-targeted`. The first Dev group's denominator retains `dev/dev-denominator.csv`; subsequent groups use `dev/<group>-denominator.csv`.

`results/raw-derived/<index>-<group>.json` contains filtered source methods and launch evidence with `derived_from`. `results/skipped-commits.csv` has `index,sha,reason`. Skipped raw records contain `index,sha,group,phase,status` with status `skipped-untouched`. Skipped build records have `build: skipped-untouched`, empty attempts, and a reason. `coverage-index-aliases.json` maps skipped predecessor indices to equivalent earlier evaluated indices.

Survival consistency adds measured/derived counts and expected counts, plus skipped indices. `raw_records` and `expected_raw_records` count measured records, including skip placeholders. `final-summary.json` adds `environment_reruns` per group and `environment_rerun_unit`. Method records that undergo environment reruns have `environment_reruns` and `environment_attempts` (initial failure, followed by retry states/messages); the first-failure CSV header is unchanged.

## Checkout lifetime and restoration

A verified successful index build is a durable sub-operation. Nonzero
`work/checkouts/<index>/` trees and their `<index>.json` markers are released
only after their last workspace consumers and coordinator validation succeed.
Prepare retains all successful builds. Survival retains the planned coverage set,
including predecessors resolved across skipped commits; other nonzero indices
are released after survival aggregation validates measured, imported and derived
records. Coverage releases each index after all groups and executable line
universes validate. Policy windows release non-boundary indices after their last
batch; regeneration boundaries retain the index through both window and
regeneration consumers. An uninterrupted run builds each index once. Restoration
is reserved for resume, legacy workspaces or genuine consumers after release.
The anchor remains through the run; successful completion releases remaining
verified successful checkouts. Failed, interrupted and in-flight indices remain.
Pool anchors are released only after completed pool publication and verification
of source, helper and resource hashes. Legacy work without recorded identities
is retained rather than pruned speculatively.

`results/workspace-identities/<index>.json` records the original successful SHA
and ordered Maven dependency paths/content SHA-256 values. Workspace restoration
never rewrites `results/builds/<index>.json` or its original build logs. New
attempts, dependency probes and diagnostics live under
`results/workspace-restorations/<index>/<attempt-id>/`. `state/workspace-<index>.json`
records running/completed/failed restoration and its diagnostics path; an interrupted
or failed restoration cannot be mistaken for a ready tree just because classes
exist. A SHA, dependency identity or build-verdict disagreement stops execution
and retains work. Old experiments remain readable; reconstructing an already
removed legacy workspace without a recorded dependency identity fails closed.
Report PNG rendering uses the experiment directory and needs no checkout.

Dependency probes write their structured payload to `dependencies.json` and
command stdout/stderr to `dependencies.log` in the probe diagnostics directory
(for example, `results/builds/logs/<index>-identity/` or the restoration attempt).
Docker Compose startup messages remain in the log. The adapter accepts only a
successful, non-timeout invocation that published a fresh payload; it never
parses the command log as JSON. Existing dependency identities remain readable.

Changed build inputs invalidate the build instead of restoring it. Before replacing
the authoritative record, the previous build, dependency identity and any restoration
state are copied to `results/build-history/<index>/<attempt-id>/{build,identity,workspace-state}.json`
(only existing files are copied). Replacement build and classpath diagnostics use
`results/builds/logs/<index>/<attempt-id>/`, preserving earlier logs in place.
The replacement record carries the new input fingerprint and may have a different
verdict or dependency identity. Interrupted attempts retain their logs and workspace;
the next attempt uses a new diagnostics directory. Build history is operational
evidence, not a scientific result.

## Successful-stage scratch cleanup

`survival run --keep-work` and `survival policy run --keep-work` retain successful
checkouts and scratch; the option propagates through background runs and pools
and is excluded from scientific/configuration fingerprints. By default the
coordinator cleans owned worker scratch after successful stage validation:
`work/phase-*`, `work/dev-method-coverage-t`, `work/validation`,
`work/import-repair`, `work/policy-survive`, and pool selection/screening scratch.
Missing/unverified outputs never authorize cleanup. Cached screening failures
retain their original diagnostics for later classification. Baseline failed
file subtrees remain whole; shared failed policy batches remain whole because
multiple methods can share their compilation and coverage evidence. Failed
developer tests also retain shared `suite-classes` and `suite-javac.txt` when
whole-suite compilation succeeded; their numbered classes folder can be empty.
Cleanup retries use the effective runner configuration, including generator
Docker overrides, even for nested pools without an `experiment.yaml`.

On a worker retry, existing scratch is copied to independent files under
`work/failed-attempts/<original-work-relative-path>/<attempt-id>/`, with a sibling
JSON record identifying the original path. A resumed worker does not erase its
interrupted attempt. Copying avoids Docker Desktop stale-directory aliases after
a host rename; the worker then clears only the original directory. These histories are not removed on successful completion.

`results/work-cleanup.json` records cumulative `removed_files`, `removed_bytes`
(logical bytes), and `retained_failures` keyed by relative work path with current
file/byte counts. Parent retained paths subsume children, avoiding double counting.
This operational report is uncompressed and excluded from scientific comparison.

After full run / verified pool completion, shared libraries, classpath jobs and
successful runtime scratch can be recreated on demand. `work/classpaths/` is retained
while any checkout remains, including legacy trees without workspace identities,
so cached builds can support baseline inventory before runner setup. It is removed
when no checkout remains. Compiler caches (including
pool-local caches), generation/policy histories, unknown work, and every generated
artifact remain untouched. KTester dataset scratch is removed only after round
output matches its generated archive and, for a baseline run, report completion
has been recorded. `work/ktester/<group>/experiment-result.json` and
`dataset_info.json` stay in place. Completed rounds are reused from
`generated/<group>/round<N>/run.json` before any worker launch or credential
access, even when cleanup removed their workspace markers. Raising `rounds.max`
rebuilds preparation/index/dataset scratch only for the new round. Existing
archived sources and responses are not rewritten; missing derived source copies
can be completed after an interrupted publication. An incomplete/failed archive
requires inspection before generation can continue.

Forcing the report stage refreshes the retained KTester completion record and its
`final_summary_sha256`. A prior completion record also identifies datasets cleaned
by older versions that removed `dataset_info.json`; imported generation without
either dataset record does not create a KTester completion record.
A partial `--until` run never triggers whole-run cleanup. Failure or interruption
leaves the owning stage's scratch; previously verified index operations may
already have released their successful checkouts.

## Compressed compile-event journal

New compilation history is stored in `results/compile-events/<index>.jsonl.gz`
(or `stage-<phase>.jsonl.gz` without an index), with a persistent matching `.lock`
file. Each append is one closed gzip member containing one JSON line. Records
have `type` (`start`, `finish`, `interrupted`, or `toolchain`) and `event_id`.
Start retains context, command and start time. Finish retains finish time,
`executed`, and the command result; interrupted retains the exception type and
available diagnostics. Toolchain records retain the JDK probe result and log.
`cache.event` and durable event log references are `<ledger-relative-path>#<id>`;
`cache.origin_event` continues to identify the originating compilation UUID.
`adapters.compile_events.read_events` and `read_event` accept both this layout
and legacy per-event directories.

The `javac`/toolchain log payload contains `text`, original `bytes`, `sha256`, and
`truncated`. Successful javac text is limited to 16 KiB of UTF-8, without splitting
a character. Nonzero exits, timeouts and exceptions retain full text. Full
successful compiler-cache logs remain in the protected compile cache.

Writers serialize using `flock`, fsync each complete member, and checkpoint its
byte boundary in the same persistent lock inode. An append after interruption
validates the uncheckpointed tail. Any damaged/incomplete tail is fsynced to a
`.jsonl.gz.damaged-<id>` evidence file before truncating back to the last complete
record. Already complete members survive a crash between append and checkpoint.
Readers expose earlier complete records even if a final member is incomplete.
A killed compile therefore remains visible as a start without a finish. Normal
appends inspect only the new tail, not the entire historical ledger.

## Bulk result compression

New writes use gzip level 6 for `results/coverage/*.xml.gz` (including
`<index>-dev-scope.xml.gz`), `results/coverage/*-lines.csv.gz`,
`results/{raw,raw-coverage,raw-derived}/*.json.gz`, and the JSON, XML and lines CSV
files inside `results/dev-method-coverage-t/`. The same rules apply inside pools.
The path names in records and the schemas above remain logical names without
`.gz`. Compression changes no uncompressed bytes. Failed partial XML may remain
plain for debugging.

`io.open_result`, `read_json` and `read_csv` read either the logical name or its
`.gz` representation. Enumeration and existence checks use the same resolution.
An interrupted conversion can leave equal twins; these count once. Conflicting
twins or corrupt gzip data raise an error rather than silently selecting stale
measurements. Writers publish a complete temporary file atomically, then remove
the previous representation. Import checksums and result hashes cover the
**uncompressed bytes**; source/resource hashes still cover the original bytes.
Rehash and stale-stage configuration hashes retain their existing meaning.

Small summaries, all state files, policy tables and direct report/Sheets inputs
remain plain. No files under `generated/` are compressed or removed. Legacy
finished experiments continue to load without migration.

Container-produced JSON and coverage CSV retain readable `0644` permissions so
Linux host coordinators can consume root-owned worker files. XML compression
preserves the original file mode. Compile-journal lock files remain persistent
and readable for archive packaging. Host-only atomic files retain their existing
private default mode.

Known pre-footprint runner fingerprints remain reusable and count toward progress.
The compatibility map pins the exact main component hashes and accounts for new
provenance entries; all unrelated component hashes still must match. Screening
reuse checks the corresponding historical key without rewriting generated
validation evidence. Unknown provenance changes still invalidate reuse.

On Linux, a host user may be unable to delete root-owned Maven/worker scratch.
Only after host deletion reports `PermissionError`, cleanup retries the same
validated descriptor-relative deletion inside the configured Docker runner.
It does not relax permissions or follow symlinks. The command result and log are
retained under `results/work-cleanup-commands/<id>.{json,log}`. A failed retry
stops cleanup and leaves the remaining evidence. This operational path is covered
by a real container fixture, including hosts where Docker remaps ownership.


## Build-discovered developer candidates

With `dev.discovery: build`, `results/dev-build-discovery.json` records the test
phase command, cwd, exit code, timeout, seconds, anchor SHA, input fingerprint,
and completion flag. `counts.run`, `counts.failed`, and `counts.skipped` count
report invocations (failed is a subset of run); `counts.methods_run` counts
distinct mapped methods with a non-skipped invocation. `counts.excluded` counts
inventory methods not run, and `counts.unmapped` counts unmapped report entries.
`excluded_by_reason` groups excluded methods. The same record appears as
`dev_build_discovery` in prepare's `results/anchor.json` and `final-summary.json`.
Regeneration pools retain their own record at their anchor commit.
`inherited_methods_run` counts recovered concrete inherited identities;
`unmapped_by_reason` groups unmapped invocations. Inherited baseline, denominator,
frozen, and policy pool records additionally retain `declaring_class` and
`declaring_file`. Their `class` is the concrete execution class, and their `file`
is its compilation source. Pool snapshots relocate the declaring-file reference
with the frozen sources. Existing records without these fields remain valid.

- `dev/dev-not-run-by-build.csv`: `class,method,kind,reason`. Reasons include
  `disabled`, `skipped`, `tag-or-group-exclusion`, `include-pattern`,
  `exclude-pattern`, `ambiguous-source-method`, and `not-reported-by-build`.
- `dev/build-report-unmapped.csv`: `report,class,name,status,reason`. Reasons
  distinguish `no-source-method` and `ambiguous-source-method`.
- `results/dev-build-reports/TEST-*.xml`: reports from the recorded discovery
  invocation. Skipped invocations are retained as evidence.

New `dev-results` imports retain `scope_selection` (scope settings, population,
and selection record) in `state/import.json`. An unchanged scope configuration
reuses that validated selection without remeasuring imported candidates.

Selection-context records additionally contain `input_sha256`, `index`,
`action` (`resolved` or `reused`), `reused_from_index` (null for resolutions),
and the cached classpath's experiment-relative path. Reused records retain the
current SHA and coordinates without rerunning Maven dependency listing.
