# test-survival

Run reproducible commit-survival and changed-line-coverage experiments for frozen developer tests and LLM-generated Java tests. Each experiment uses one YAML configuration and keeps its evidence in an experiment folder.

## 1. Install

Install the CLI from this repository with uv. Replace `/path/to/test-survival` with your checkout.

```sh
uv python install 3.12
uv tool install --editable /path/to/test-survival
uv tool update-shell
```

Open a new shell after updating its path. The `survival` command then works from any directory without activating a virtual environment.

For local development, use a `.venv` instead:

```sh
cd /path/to/test-survival
uv venv --python 3.12 .venv
uv pip install -e '.[dev]'
source .venv/bin/activate
```

The development install includes pytest:

```sh
pytest -q
```

The CLI requires Python 3.12 or newer and PyYAML. Docker supplies the Java tools.

Java/Maven projects support JUnit 4 and JUnit 5. JavaScript project and generator combinations are recognized but are not implemented in this version.

## 2. One-time setup

Build a runner image for each JDK version your target projects need. For JDK 25, run this from the test-survival checkout:

```sh
cd /path/to/test-survival
docker build --build-arg JDK_VERSION=25 -t survival-java:25 docker/java
```

Build the image once and reuse it across experiments. Change the build argument and tag when you need another JDK version.

Image mode mounts the named Docker volume `survival-m2` at `/root/.m2` by default. Maven downloads survive container removal. Set `runner.docker.m2_volume` to use a different named volume; Compose keeps its own volume configuration.

An explicit bind mount at `/root/.m2` or a parent directory takes precedence over the named volume.

All Java builds, inventories, test launches, and JaCoCo runs execute inside Docker. The CLI and native model client run on the host.

Store your API key in an env file outside the repositories, such as `/absolute/path/to/private/model.env`. Never commit this file.

```dotenv
OPENROUTER_API_KEY=replace-with-your-key
```

Point each experiment at that file:

```yaml
model:
  provider: openrouter
  name: openai/gpt-oss-120b
  env_file: /absolute/path/to/private/model.env
  env_key: OPENROUTER_API_KEY
```

Credentials are read privately and excluded from model-call records. An environment variable with the configured key name takes precedence over the file.

Omit `model.env_file` to read credentials only from the process environment. Host paths beginning with `~` expand to your home directory when loaded; result-affecting paths retain their written spelling in configuration hashes.

KTester also needs the existing KTester author tool and its container dependencies. Create a Compose override for each target project, alongside that fork's experiment configuration or Docker files.

For example, `docker/compose.survival.yml` in the target fork can map the project and author-tool checkouts:

```yaml
services:
  ktester:
    volumes:
      - /absolute/path/to/target-fork:/repo
      - /absolute/path/to/KTester:/work
```

Use the KTester base Compose file followed by this override in the generator's configuration:

```yaml
generators:
  - name: ktester
    method: ktester
    impl: author-tool
    tool_path: /absolute/path/to/KTester
    docker:
      compose_files:
        - /absolute/path/to/KTester/docker/compose.yml
        - /absolute/path/to/target-fork/docker/compose.survival.yml
      service: ktester
      mounts:
        /absolute/path/to/target-fork: /repo
        /absolute/path/to/KTester: /work
```

A generator's `docker` block replaces `runner.docker` for that generator. This lets KTester use its JDK 17 environment while the main runner uses a newer JDK.

Keep the host-to-container paths in `mounts` consistent with the Compose volumes. The CLI uses these mappings to translate paths in commands.

## 3. Where experiments live

Put each experiment in `experiments/<name>/` inside the target project's fork:

```text
target-fork/
  experiments/
    first-run/
      experiment.yaml
      state/
      population/
      generated/
      dev/
      results/
      work/
```

This keeps the configuration and results with the project they describe. Publish the results on that fork's experiment branch.

The CLI takes the experiment folder path and does not depend on the current directory. The examples below use `/absolute/path/to/target-fork/experiments/first-run`; replace it with your folder.

## 4. First experiment

Create the commented configuration:

```sh
survival init /absolute/path/to/target-fork/experiments/first-run --language java --build maven
```

Edit `experiment.yaml`. Set `project.repo` to the fork's absolute path and `project.module` to the Maven module directory, or `.` for the root.

Set `anchor` to the starting tag or commit. Set `range.end` to a fixed endpoint for a reproducible range.

Choose your generator groups and model settings. The template includes KTester; configure its tool path and Compose override, or replace it with a supported native generator.

Select the runner image you built:

```yaml
runner:
  docker:
    image: survival-java:25
    compose_files: []
    mounts:
      /absolute/path/to/target-fork: /repo
```

Run preparation and scope selection before making model calls:

```sh
survival run /absolute/path/to/target-fork/experiments/first-run --until scope
survival show /absolute/path/to/target-fork/experiments/first-run scope
```

If the anchor is outside the endpoint's first-parent history, prepare names the first containing first-parent commit. Its error also reports whether that commit's module tree matches the anchor.

Review the selected classes and focal method counts. Adjust the `scope` settings in the YAML if needed, then repeat:

```sh
survival run /absolute/path/to/target-fork/experiments/first-run --until scope
survival show /absolute/path/to/target-fork/experiments/first-run scope
```

When only scope settings change, prepare is skipped. Scope selection runs again.

Use `scope.rule: all-eligible` to select every class satisfying `min_methods` and `exclude_accessor_heavy`. This rule ignores `target_methods` and `max_classes`; ranking still determines output order.

Developer groups can use project-wide, name-targeted, or per-method coverage-scoped denominators. A derived group filters another developer group's survival records and measures its own coverage.

Use `range.skip_untouched` to skip irrelevant commits for every group while retaining commit indices. Use `survival.environment_errors` to retry failures whose messages match configured patterns.

Start the remaining work in the background:

```sh
survival run /absolute/path/to/target-fork/experiments/first-run --background
```

The command prints the process ID and background log path. Stages run in this order:

```text
prepare → scope → generate → screen → control → survive → cover → report
```

Screening keeps methods that pass every configured anchor run and removes exact whitespace-stripped duplicate bodies.

Survival uses a fixed denominator: a lost method never returns. Build failures carry survivor counts forward.

Coverage uses the methods alive entering each index. It measures changed production commits and their predecessors.

Reports include Markdown tables, an SVG figure, raw records, consistency checks, and a summary JSON file. A PNG is also produced when the JDK renderer succeeds.

## 5. Watching a background run

Follow the stage log from the experiment folder:

```sh
cd /absolute/path/to/target-fork/experiments/first-run
tail -f state/log.txt
```

Watch a two-line progress panel from any directory (refreshes every five seconds,
exits when the run finishes or you press Ctrl+C):

```sh
survival status /absolute/path/to/target-fork/experiments/first-run --watch
```

You can also run status once, or inspect a saved stage summary:

```sh
survival status /absolute/path/to/target-fork/experiments/first-run
survival show /absolute/path/to/target-fork/experiments/first-run screen
```

Status and stage log lines show elapsed time, completed/total items, and an estimated finish time. Prepare counts builds including the anchor; generate counts model calls; screen counts files; survive counts commits; cover counts indices.

Foreground runs automatically reserve the bottom two terminal rows for the same
panel when stderr is a TTY, with logs scrolling above. Use `--no-panel` on `run`
or `status --watch` to disable it. The panel shows
stage progress, percent, remaining time, elapsed time, and estimated finish;
`+` marks completed stages, `>` the current stage, `-` pending stages, `~` stale
completed stages after a configuration change, and `!` failures. Watch mode prints
a final plain status after restoring the terminal, including when stopped with Ctrl+C.
Its units are builds, items, methods, files, methods, commits, indices, and tables
for prepare through report. Generation counts focal methods per round/prompt,
with calls as a secondary count. KTester counts completed case generation or
repair from its output directory, not initial Java frameworks or repair calls.
While an author tool runs, `state/log.txt` also receives a plain progress line every
two minutes with methods done/total, responses so far, and the class under repair.
The panel abbreviates repair class names to fit alongside the metrics or stage
marks. For older workers without per-method repair records, the active class is
unknown: their log contains repair starts but cannot establish when a class finished.
Watching an already-running experiment only reads its evidence; the periodic
log lines require the run itself to have started with this version.

Panel detection uses stderr: redirecting stderr disables the panel, while piping
stdout alone leaves it enabled. Stdout log lines contain no ANSI controls. If you
echo stdout back to the same terminal with `tee`, use `--no-panel` so that `tee`'s
independent terminal writes cannot interfere with panel rendering.

The estimate uses that stage's throughput in the current attempt. Cached records count toward completion but not toward the current attempt's throughput.

The estimate is unknown until there is enough evidence or when the total is unknown. Adaptive KTester call totals remain unknown until generation stops.

## 6. Stopping and resuming

Stop the recorded run:

```sh
survival stop /absolute/path/to/target-fork/experiments/first-run
```

Stop verifies the saved process identity and terminates the coordinator, its workers, and the run's Docker containers. It marks the running stage failed with `stopped by user` and permits resumption only after cleanup is confirmed.

`survival stop` uses SIGSTOP/SIGKILL, which prevents foreground panel cleanup.
If you stop a foreground run from another terminal, run `reset` in the original
terminal to restore normal scrolling and clear stale panel rows. The same recovery
applies after an OOM or other hard kill. Ctrl+C and SIGTERM allow normal cleanup.

If cleanup fails, resumption stays blocked. Restore Docker access if needed and repeat the stop command to finish cleanup.

A live run started by an older version may lack a saved process identity. Stop refuses to signal that PID because it cannot verify ownership.

Only one process may run an experiment at a time. Resume with the same run command:

```sh
survival run /absolute/path/to/target-fork/experiments/first-run --background
```

Completed stages with matching configuration hashes are skipped. An interrupted stage keeps the evidence below.

| Stage | What is kept and what happens on resume |
| --- | --- |
| prepare | Build records, checkouts, and baseline evidence remain. Valid cached work can be reused. |
| scope | Written surveys and denominator evidence remain. Scope selection reruns. |
| generate | Finished rounds and recorded model calls remain. Completed, current groups are skipped; other groups revisit their rounds using saved evidence. |
| screen | The stage reruns to rebuild funnels and frozen denominators. Valid per-file validation records can be reused. |
| control | The stage reruns the anchor control check. Valid cached group records can be reused. |
| survive | Per-commit, per-group records remain. Valid records are reused and survival summaries are rebuilt. |
| cover | Per-commit coverage records remain, including anchor records when requested. Valid records are reused and coverage summaries are rebuilt. |
| report | Written report files remain. Report generation reruns. |

If a model call was interrupted before its response was recorded, inspect the call ledger before issuing another call. The tool does not automatically retry an unknown outcome.

Stage exceptions are saved in `state/<stage>-error.txt`. The failure log line names the file, including when no other evidence exists yet.

## 7. Changing configuration after a run

The CLI hashes result-affecting YAML settings as written, before filling defaults.
Changes invalidate the affected stage and all later stages. Scheduling and host
wiring do not affect stage hashes: `runner.workers`, `runner.reuse`,
`runner.docker.mounts`, `runner.docker.env_file`, and `model.env_file` are ignored.
Docker mounts and env files in generator overrides are ignored as well.
`runner.docker.image`, `runner.docker.compose_files`, timeouts, and JVM settings
remain in the hash. Build, raw test-run, and per-method coverage cache keys also
ignore scheduling and host wiring. Runner implementation/provenance changes can
still invalidate cached measurements.

Run `rehash` **once on experiments created before this hash-policy change**, after
any active run has finished and before changing result-affecting configuration:

```sh
survival rehash /absolute/path/to/target-fork/experiments/first-run
```

This recomputes only `config_hash` in completed stage state files using the current
configuration. It preserves failed/pending stages, other state fields, group and
round hashes, evidence, and logs. It refuses to run while the experiment lock is held,
and requires the existing `state/run.lock` created by a run (never deletes or replaces
that lock inode). Recognizable legacy completed-stage hashes stop a normal resume
with a `rehash` instruction before any stage is invalidated. When generation is
subsequently entered, compatible legacy group/round ledgers are reused; forcing one
group does not force unrelated groups to generate again.
It does not validate or rerun results; subsequent result-affecting configuration
changes should go through normal stage invalidation.

| Changed setting | Earliest affected stage |
| --- | --- |
| `project`, `anchor`, `range`, result-affecting `runner` settings, or `dev.stable_runs` | prepare |
| `scope`, other `dev` settings, or generator names and methods | scope |
| `model` or other generator settings | generate |
| `screen` | screen |
| `survival` | survive |
| `coverage` | cover |
| Other report inputs | report |

When a developer group uses `coverage-scoped`, changes to `survival.environment_errors` also invalidate scope. That stage uses those retry settings during denominator selection.

Use a normal run to process stale stages and their successors. Use `--until` to stop after a selected stage:

```sh
survival run /absolute/path/to/target-fork/experiments/first-run --until scope
```

Use `--stage` to run only one stage. Every earlier stage must already be completed with its current configuration:

```sh
survival run /absolute/path/to/target-fork/experiments/first-run --stage survive
```

Add `--force` to execute the selected stage even when it is marked complete. Stage-level caches may still reuse valid records:

```sh
survival run /absolute/path/to/target-fork/experiments/first-run --stage survive --force
```

Use `--group` with `--stage generate` to select one configured generator group:

```sh
survival run /absolute/path/to/target-fork/experiments/first-run --stage generate --group ouedraogo --force
```

Forced generation archives the group's old generated directory under `work/generation-history/` before regenerating it. Run the pipeline again afterward to process downstream stages.

## 8. Replaying archived generation

Create a separate replay experiment and edit its YAML to match the archived inputs and generator group names:

```sh
survival init /absolute/path/to/target-fork/experiments/replay
```

Import generated artifacts to replay them without new model calls:

```sh
survival import /absolute/path/to/target-fork/experiments/replay --from /absolute/path/to/reference --what generated
```

Optionally import the archived stable developer baseline:

```sh
survival import /absolute/path/to/target-fork/experiments/replay --from /absolute/path/to/reference --what dev-baseline
```

Run the replay:

```sh
survival run /absolute/path/to/target-fork/experiments/replay
```

Imports record copied-file hashes and synthesize round manifests from archived directories and funnels. See the [reference replay configuration](examples/jsv-run2/experiment.yaml) for a complete example.

For weekly reruns, reuse completed developer measurements while regenerating LLM tests.
Prepare the destination first so both experiments have resolved anchors, commit
sequences, and frozen test snapshots, then import:

```sh
survival run /absolute/path/to/target-fork/experiments/weekly --until prepare
survival import /absolute/path/to/target-fork/experiments/weekly --from /absolute/path/to/previous-experiment --what dev-results
survival run /absolute/path/to/target-fork/experiments/weekly
```

Before copying evidence, `dev-results` requires identical project repo paths,
anchor SHAs, ordered commit SHAs in `results/commits.csv`, `skip_untouched`, and
`dev/src-test-t` tree hashes (relative file paths and contents). It refuses an
incompatible import with the differing field named in the error. Result-affecting
runner/runtime settings, project module and test framework, baseline stability runs,
survival rules, and coverage settings must also match. Scheduling and host wiring
may differ. Archived generation/report options are not validated; unknown measurement
options are refused instead of silently assuming compatibility.

Every Dev group with the same name and denominator rule is reused. The import
retains the already-identical frozen test tree, copies shared baselines, maps each
matching group's denominator by name, and copies its raw survival, derived survival, coverage XML/line maps,
raw coverage, and control records. Unmatched groups retain their denominators.
Scope and screen retain the imported frozen populations. Control, survive, and
cover skip imported Dev groups and run the remaining groups; report recomputes
aggregates from both imported and newly measured records.

`state/import.json` records the source experiment, compatibility values, groups,
file mappings, and checksums of the destination bytes. `state/prepare.json` records
the imported baseline, which prepare reuses after validating its compatibility and
checksum. Digest caches use file identity, size, mtime, and ctime, so unchanged files
are not repeatedly read during validation or progress refreshes. The source is
read-only and is not needed after the copy. Imported evidence is checked before reuse; missing, changed, or incompatible
records are refused instead of silently skipping a measurement. Use a completed
source with all records needed by the destination. `results/final-summary.json`
records the source under `imported_dev_results`. The import invalidates scope
and all later stage state so population summaries, LLM subsampling, funnels, and
reports are rebuilt using the imported baseline and denominators.

## 9. Dashboard

Create an `experiments.yaml` file with a list of experiment paths, titles, and layouts:

```yaml
experiments:
  - path: /absolute/path/to/target-fork/experiments/first-run
    title: First experiment
    layout: unified
```

Supported layouts are `unified`, `per-group`, and `testpilot-ss`. See [examples/experiments.yaml](examples/experiments.yaml) for examples of historical layouts.

Build the dashboard:

```sh
survival dashboard build --experiments experiments.yaml --out dashboard
```

Open `dashboard/index.html` in a browser. It is a single offline file and works from `file://`.

## 10. Further reading

Read [docs/design.md](docs/design.md) for configuration details, stage behavior, and generator design.

Read [docs/result-formats.md](docs/result-formats.md) for saved evidence schemas and report formats.
