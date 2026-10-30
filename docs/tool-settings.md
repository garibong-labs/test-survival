# User tool settings

Terminal log timestamps, `survival status`, and live progress/ETA use **Asia/Seoul**
(UTC+09:00) by default. This is a presentation preference, independent of every
`experiment.yaml`. The CLI commands and experiment schema are unchanged.

To override it, create `~/.config/test-survival/config.toml`:

```toml
[display]
timezone = "Asia/Seoul"
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

## Policy coverage JVM heap

`SURVIVAL_JVM_JACOCO` overrides `runner.jvm.jacoco` for policy coverage JVM
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
It does not affect compilation, fallback test launchers or baseline coverage.
Policy survival instrumentation is restricted to packages from the saved scope;
each JVM processes at most eight snapshot groups, sequentially. Failed chunks
fall back independently, preserving successful chunks' coverage.

Coverage version 3 recomputes version-2 policy records with missing coverage on
resume without `--force`. Version-2 complete coverage, completed pools, imported
run records and carry records remain reusable. Survival, coverage and reports
refresh through the existing version check. Older pre-version-2 executions are
still upgraded.

Baseline survival evaluates commits in order within each group (groups may run
in parallel). Policy survival also waits for each index's outcomes before
requesting the next index. Only methods alive on entry are requested; a physical
method shared by policies is requested if any policy still needs it. Later
records retain dead methods as `state: dead`, with
`message: not executed; failed at index N`, keeping the full denominator.
Unselected members of a partially selected policy pool are recorded as
`not-selected`. Neither marker is a new first failure.

Baseline `input_sha256` still uses the full frozen method list. Existing PR #21
survival records retain compatibility through an exact provenance check for the
orchestration files changed here; other runner provenance must still match.
Cached records are read unchanged and reconstruct alive-entry sets on resume.

New policy processes inherit the environment of the shell that starts them.
For a dnsjava baseline command chained into `survival policy plan` and
`survival policy run`, those new processes use the merged code and the variable
if it is already exported in their launching shell. Exporting it in a different
shell does not change an already-running process's environment.
An already-running baseline coordinator also keeps its loaded orchestration
code; alive-entry dispatch applies when survival starts in a new process.
