# User tool settings

Terminal log timestamps, `survival status`, and live progress/ETA use **Asia/Seoul**
(UTC+09:00) by default. This is a presentation preference, independent of every
`experiment.yaml`. The CLI commands and experiment schema are unchanged.

To override it, create `~/.config/test-survival/config.toml`:

```toml
[display]
timezone = "Asia/Seoul"

[archive]
destination = "/Users/donggi/Library/CloudStorage/GoogleDrive-hamjoon@gmail.com/내 드라이브/_KUMaster/졸업논문/experiment-projects"
```

Other examples are `"UTC"`, `"America/New_York"`, and `"Asia/Kathmandu"`. Values
must be IANA time zone names available in the operating system's time zone database.
Offsets follow the selected zone's rules, including daylight saving time.

## Path and precedence

1. If `XDG_CONFIG_HOME` is nonempty, read
   `$XDG_CONFIG_HOME/test-survival/config.toml`. It must be an absolute directory.
2. Otherwise, read `~/.config/test-survival/config.toml`.
3. A missing file, empty file, or absent `display.timezone` uses `Asia/Seoul`.
   The selected file's value overrides that default. Files at the two paths are
   never merged; selecting XDG does not fall back to the home config file.

The tool does not create this file automatically. There is no project-level
override, experiment setting, timezone flag or special timezone environment
variable. Changing the process-wide `TZ` is unnecessary and does not control this
display preference.

Settings are read once at command startup and shared with the run's logging and
progress threads, including all import log messages. Editing the file affects
the next command, including a new `status --watch`, and does not change an
already-running command. Do not restart
an experiment just to change its display: a new watcher can show its existing
UTC evidence in the desired zone. Already-running older tool versions retain
their old output behavior.

Invalid TOML, unreadable files, unknown keys, wrong value types, unavailable time
zone names, or a relative `XDG_CONFIG_HOME` produce a `survival:` error and exit
code 2 before command work starts. No fallback silently conceals an invalid
setting. A missing system time zone database is reported as an unavailable zone;
install the OS time zone data or use an environment that provides it.

## Display and storage contracts

An instant such as `2026-10-29T06:48:48+00:00` displays as
`2026-10-29T15:48:48+09:00` under the default. Timestamps and ETAs include their
numeric UTC offset. Narrow live panels keep the full date, minute and offset;
they abbreviate other metrics first. Extremely narrow panels say `ETA hidden`
instead of showing a truncated timestamp. Compact units use `b` builds, `m`
methods, `f` files, `c` commits/calls, `i` indices/items and `t` tables; `r` and
`e` label remaining and elapsed durations, and a separate `c` field counts calls.

UTC remains canonical for internal arithmetic, `state/*.json`, `state/log.txt`,
compiler events and experiment results. Elapsed/remaining durations are unchanged.
The same progress calculation produces both the localized console line and the
UTC saved line, so logging does not scan experiment evidence twice.
The `Last log:` row in status/watch converts the saved line's timestamp and
structured progress ETA for display only. Other message text is preserved;
unstructured or incomplete legacy lines remain readable without changing the file.

`survival run --background` redirects its child stdout to `state/background.log`.
That internal child deliberately uses UTC and does not load display settings;
both saved logs therefore remain UTC even when the foreground default is Seoul.
The parent command still validates the user settings before starting the child.
Use `survival status` or `survival status --watch` for localized background progress.
Reading the raw saved files with `cat` or `tail` shows their stored UTC values.

Foreground stdout remains a display stream even when piped. A user-created
`tee` capture therefore contains the displayed time zone; the tool-managed
`state/log.txt` is still UTC. Live terminal output is on stderr as before.

Display settings do not enter the experiment configuration, fingerprints,
compiler environment, result provenance or resume decision. Changing the display
zone requires no rehash, migration, cache clearing or experiment rerun. Existing
records are read as stored and are never rewritten to change their timezone.

## Archive destination

`archive.destination` is an optional nonempty directory path in the same user
settings file. `survival archive EXP_DIR --dest DIR` overrides it. If neither is
provided, the command stops with an error. The destination root must already
exist; the command creates only its fork subdirectory. Paths expand `~` and are
resolved with `Path.resolve()` and checked with `exists()`, allowing macOS's
Unicode normalization of Korean Google Drive folder names.

Archive settings follow the same XDG precedence and strict validation as display
settings. They are loaded once at command startup and never change experiment
configuration or provenance. `[archive]` supports only `destination`; a missing
table is valid and does not affect other commands. No user settings file is
written automatically.

## Batch and policy coverage JVM heap

`SURVIVAL_JVM_JACOCO` overrides `runner.jvm.jacoco` for policy and batch-baseline coverage JVM
launches (`policy-survive` and pool `policy-cover`) without editing experiment
configuration. Use a Java heap size such as `3g`:

```sh
export SURVIVAL_JVM_JACOCO=3g
cd /Users/donggi/_projects/experiment-projects/json-schema-validator && survival policy run experiments/regen-policies
```

The host forwards the value to the container worker, which reads it when
launching coverage. Unset or empty values use the configured heap. The original
`jvm` settings and optional `coverage_environment` are saved in the worker's
`*-job.json`; `coverage-jvm.json` and the batch result record the configured and
effective heap and environment override. Each chunk's command, manifest and log
are recorded in the batch result's `runs` list, with logs under
`work/policy-survive/<index>/chunks/<chunk>/console.txt`.

The override never enters stage hashes, build fingerprints or the policy digest.
It does not affect compilation, fallback test launchers or legacy baseline coverage. Batch baseline launches use this override and
record their logs under `work/baseline-batches/<index>/`.
Policy survival instrumentation is restricted to packages from the saved scope;
each JVM processes at most eight snapshot groups, sequentially. Failed chunks
fall back independently, preserving successful chunks' coverage.

Coverage version 3 recomputes version-2 policy records with missing coverage on
resume without `--force`. Version-2 complete coverage, completed pools, imported
run records and carry records remain reusable. Survival, coverage and reports
refresh through the existing version check. Older pre-version-2 executions are
still upgraded.

With `survival.selection: all`, baseline and policy survival dispatch up to
`runner.workers.survive` indices/jobs concurrently. Each dispatch uses the failures
confirmed by already-completed earlier jobs for that group or policy union.
A lagging snapshot may request extra
methods that an earlier in-flight index will later mark dead. Ordered aggregation
ignores those extra observations and retains exact alive-entry sets and first
failures. Policy dispatch stops at regeneration boundaries so reset/fill uses
the exact prior outcomes; a physical method shared by policies is requested if
any policy may still need it. Records retain omitted methods as `state: dead`, with
`message: not executed; failed at index N`, keeping the full denominator.
Unselected members of a partially selected policy pool are recorded as
`not-selected`. Neither marker is a new first failure.

Baseline `input_sha256` still uses the full frozen method list. Existing PR #21
survival records retain compatibility through an exact provenance check for the
orchestration files changed here; other runner provenance must still match.
Cached records are read unchanged and reconstruct alive-entry sets on resume.
A record may have executed more methods than the current exact alive-entry set;
it remains reusable if every method it omitted is still dead (or unselected).
If an omission needs an earlier in-flight result to establish this, dispatch
waits for that result instead of needlessly recomputing a valid cache.

New policy processes inherit the environment of the shell that starts them.
For a dnsjava baseline command chained into `survival policy plan` and
`survival policy run`, those new processes use the merged code and the variable
if it is already exported in their launching shell. Exporting it in a different
shell does not change an already-running process's environment.
An already-running baseline coordinator also keeps its loaded orchestration
code; alive-entry dispatch applies when survival starts in a new process.


## Baseline execution and screening runs

`survival.execution` accepts `legacy` (default) or `batch`. Batch uses the policy
worker for control and survival, with scope-only line coverage in the same pass.
The `cover` stage aggregates existing lines without Java test execution. Batch
ignores `survival.flaky_rerun` and `survival.environment_errors` for measurement;
these settings continue to apply to legacy measurement and their existing
preparation/selection consumers. An omitted execution setting retains legacy
fingerprints. Changing the mode invalidates control and later stages.

`screen.stable_runs` controls generated-test screening, including generator
pools. `dev.stable_runs` controls developer stability, including developer pool
screening after full-suite coverage selection. Neither pool count is fixed at
three. Defaults remain three for old configurations. Changing a run count is a
measurement-input change; completed pools are not silently rescreened.

`policies.set[].developer` accepts `regenerate` or `keep`. Omission uses the global
`policies.regenerate_developer` value. Set `policies.groups: [dev-scope]` (or your
developer group name) for a developer-only pool exercise; policy run has no
`--group` flag. Always use a separate policy output directory from its baseline.

## Affected selection

Set `survival.selection: affected` in `experiment.yaml`; the default is `all`.
Baseline affected selection requires `survival.execution: batch`. A policy's own
selection setting controls its execution, independently of its baseline's mode;
segment 1 and non-regenerating references continue to read the baseline.

The compiler trigger conservatively compares all repository `pom.xml` files at
last-execution and current SHAs: parent versions, `maven-compiler-plugin` settings
(including profile/execution configuration), and properties whose names contain
`compiler`, `release`, `source`, `target`, or `java.version`. Cosmetic changes and
unrelated POM fields alone do not trigger execution. Unknown XML/diff evidence
executes conservatively. No effective Maven model is extracted.

Resolved production/test-scope dependency entries are compared after the worker's
JUnit exclusions and runner-library additions, in classpath order, using group,
artifact, version, classifier and type. Project output directories are excluded.
Affected preparation bypasses the module-POM-only classpath cache and resolves
per built commit. Unresolved coordinates force execution.

Affected mode measures indices in order and limits concurrent chunks within each
index to `runner.workers.survive`. Each chunk has isolated filesystem resources.
`all` retains commit-level concurrency. Use the same worker setting for timing
comparisons. `runner.jvm.jacoco`/`SURVIVAL_JVM_JACOCO` still set **each JVM's** heap
limit, not an aggregate memory budget across concurrent chunks.

New batch and policy compilations use iterative error-file removal in both
selection modes. Affected compiles executed test sources plus non-test helpers;
all compiles the eligible root. `-Xmaxerrs` is set to 2147483647, diagnostics keep
source associations, and each retry uses a clean output directory. No frozen
sources or completed experiment artifacts are removed.


## Multi-module projects

When `project.module` names a submodule, every Maven command that resolves or
builds the module runs at the checkout root with `-pl <module> -am`: dependency
prefetch, classpath resolution, the per-commit compile, baseline test runs, the
affected-selection dependency listing, and KTester dependency copying. Sibling
modules between releases carry SNAPSHOT versions that exist in no repository;
Maven finds them only inside the reactor. Goals that list the module's
dependencies (`dependency:build-classpath`, `dependency:list`,
`dependency:copy-dependencies`) first run `-Dmaven.test.skip=true package` in the same
invocation, so each sibling resolves to the jar built from that commit's own
checkout, for example `common/target/<artifact>-<version>.jar`.
Test compilation is skipped there too, so a commit whose own tests no longer
compile still resolves. A dependency on a sibling's `test-jar` is not supported.

Because such a classpath names files inside one index's checkout, a submodule
classpath is resolved per index and never shared through `pom-sha256`. A
root-module project (`project.module: .`) keeps its earlier commands, cache keys,
and record fingerprints.

`range.skip_untouched` and affected selection watch only the configured module's
own paths and every `pom.xml`. A commit that changes only a sibling module's
`src/main` is skipped as untouched, and affected selection does not rerun a test
because a sibling's sources changed.

## Developer discovery and scope bounds

These are experiment YAML settings, shared by every project:

| Setting | Default | Behavior |
|---|---|---|
| `dev.discovery` | `source` | `build` restricts candidates to inventory methods actually executed by Maven Surefire. New experiment templates set `build`. |
| `scope.max_dev_scope` | `400` | Maximum Dev(scope) union for ranked scope selection. Positive integer. |

Build discovery executes `mvn -o test -Dmaven.test.failure.ignore=true` at t.
For a configured submodule it invokes the reactor with `-pl <module> -am`, and
reads only that module's `target/surefire-reports/TEST-*.xml`. Prefetch the
project's dependencies and Surefire provider into the runner's Maven cache before
an offline run. A nonzero phase exit or timeout stops preparation; it does not
produce an empty population. Discovery and every stability repeat require at
least one Surefire XML report. Custom report destinations, disabled XML reports,
or test phases that produce no reports are currently unsupported and stop with
an explicit error. An empty saved report directory is not reusable evidence.
Reports containing only skipped cases can establish an empty candidate set.
Failed methods still count as executed candidates,
and the existing stability screen determines denominator eligibility. Parameterized
and repeated invocations collapse to their source method. Skipped-only methods
and unmappable report entries do not enter the candidate set.
Source inventory expands inherited tests through concrete subclasses, including
multi-level inheritance and nested classes. Generic superclass/interface bindings
are applied when matching overrides; compiler-generated bridges do not execute
as additional tests. Jupiter includes non-private
superclass tests across package boundaries, including package-private tests. Candidate filtering, denominators,
survival execution, and regeneration pools use the concrete class/method pair.
`declaring_class` and `declaring_file` retain the method's source declaration;
the execution `file` retains the concrete class's inventoried source path, even
when several top-level classes share a file. Unresolved external
superclasses and ambiguous overloaded methods remain audited exclusions.

Exclusion reasons use skipped reports and literal module configuration/source
annotations when available. Maven does not explain every omitted test; uncertain
causes are recorded as `not-reported-by-build`, not guessed. The discovery phase
provides the first stability observation; additional configured stability runs
retain the existing Maven baseline behavior. Regeneration runs discovery at its
own commit before selecting pool candidates.
Build-discovered pool coverage selects candidate methods explicitly, so an excluded
slow or tagged method in the same class is not executed during pool measurement.

Ranked selection checks the union of stable, passing developer methods that cover
a selected source file (`coverage-scoped`) or match the existing class-name rule
(`scope-targeted`). When several scoped groups are configured, every such group
must fit the bound. Project-wide groups do not constrain Dev(scope).
`explicit` and `all-eligible` scopes ignore this bound. Imported completed
populations keep their saved denominators and scope-compatibility checks. A Dev
result import preserves the already validated scope and skipped-class record when
scope settings are unchanged; it does not rerank after removing imported groups
from the measurement set.

For source discovery, coverage measurement retains the existing stable-only
population; audited ambiguous overloads and other unstable source rows are not
executed. Build discovery measures its full candidate set, then applies stability
and passing-state requirements to the denominator.

The coverage worker selects only candidate methods, with at most 256 methods per
snapshot group and eight groups per JVM chunk. It compiles the frozen suite once
and records coverage of the entire production tree. Candidate methods use separate
class loaders inside each JVM so discovery, shared setup, static initialization,
and teardown contribute to every method that executes them. This avoids carrying
one method's body hits into another method. Unchanged candidate/source/runner
inputs reuse this measurement when only scope settings change. Class selection uses this
one measurement; it does not launch a JVM for each tentative class or method.


Saved `status` and `show` output does not require a runnable configuration for
pre-framework completed experiments. Policy `status`/`--watch` reads saved policy
state without reopening its historical baseline configuration. Running or planning
an experiment still requires valid configuration and baseline inputs.
