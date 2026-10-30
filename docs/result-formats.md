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

`dev/dev-denominator.csv`: `class,method,kind`.

`population/class-survey.csv`: `class,file,m,has_test,d,c` where `m` is the focal method count, `has_test` `yes`/`no`, `d` the dedicated-test method count, `c` the number of commits in range that change the class.

`population/ktester-population.csv`: `id,class,method-name,test-class,test-path`. `method-name` is the signature with parameter types, for example `from(ValidationContext, String, URI, JsonNode, JsonSchema, boolean)`.

`results/scope-selection.json`: `classes`, `m`, `developer_denominator`, `eligible_classes`, `mandatory_stop`, `stop_reason`, `model_calls`.

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

## State and imports

`state/<stage>.json`: `status`, `config_hash`, `started`, `finished`, `notes`. Status is `pending`, `running`, `completed`, or `failed`; the CLI reports `stale` for completed hashes that differ. Group-only generation also records `group_hashes`.

`state/run.json`: `pid`, `log`, `status`, `started`, `finished` when finished. `state/run.lock` is the advisory process lock. `state/log.txt` contains timestamped stage summaries.

`state/import.json`: keyed by `generated` and/or `dev-baseline`; each entry contains `source`, `files` (relative path to SHA-256), `source_sha256`, and `imported`.

`generated/<group>/rounds.json`: `source: imported` and `source_path` for imported data, or `config_hash` for new generation; `rounds` contains `round`, `directory`, `funnel`, and optional `model_calls`/`source`; `stopped` records the stop reason. Imported directories each get an independent ordinal `round`; `archived_round` and `archived_retry` retain the original numbering, with legacy `roundN-retry` mapping to archived retry 1.

Funnel JSON records `group`, `round`, `files`, `compiled`, `methods`, `stable_3_of_3`, `duplicates`, `kept_this_round`, `total_kept`, `developer_denominator`, `ratio_to_dev`, `per_prompt`. The legacy `stable_3_of_3` name means all configured screening runs. Compile-failure records contain `file`, `category`, and `diagnostic`.

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

`results/compile-events/<unique-id>/` is independent of replaceable worker scratch and
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

`results/dev-method-coverage-t.csv`: `class,method,scope_lines_covered,scope_files,total_lines_covered,seconds,status`. `scope_files` is a JSON array of scope file paths. The reference addendum calls this column `scope_files_touched`; comparisons accept that historical spelling. Raw per-method JSON, JaCoCo XML, and executable-line CSVs are under `results/dev-method-coverage-t/<class>#<method>.*`.

`dev/<group>-excluded.csv`: `class,method,kind,status,scope_lines_covered,reason`. Reasons are `failed-alone`, `no-scope-line-covered`, or `not-scope-targeted`. The first Dev group's denominator retains `dev/dev-denominator.csv`; subsequent groups use `dev/<group>-denominator.csv`.

`results/raw-derived/<index>-<group>.json` contains filtered source methods and launch evidence with `derived_from`. `results/skipped-commits.csv` has `index,sha,reason`. Skipped raw records contain `index,sha,group,phase,status` with status `skipped-untouched`. Skipped build records have `build: skipped-untouched`, empty attempts, and a reason. `coverage-index-aliases.json` maps skipped predecessor indices to equivalent earlier evaluated indices.

Survival consistency adds measured/derived counts and expected counts, plus skipped indices. `raw_records` and `expected_raw_records` count measured records, including skip placeholders. `final-summary.json` adds `environment_reruns` per group and `environment_rerun_unit`. Method records that undergo environment reruns have `environment_reruns` and `environment_attempts` (initial failure, followed by retry states/messages); the first-failure CSV header is unchanged.
