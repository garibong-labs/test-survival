# test-survival

Run reproducible commit-survival and changed-line-coverage experiments for frozen developer tests and LLM-generated Java tests. The `survival` CLI uses one YAML configuration and retains per-command evidence in an experiment folder.

```sh
uv python install 3.12
uv venv --python 3.12 .venv
uv pip install -e '.[dev]'
source .venv/bin/activate
survival init /path/to/project/experiments/my-experiment --language java --build maven
```

Edit `experiment.yaml`: set the project checkout, anchor, fixed range endpoint, generators, and Docker image or Compose files. Paths in `runner.docker.mounts` map host locations to container locations. All Java builds, inventories, test launches, and JaCoCo runs execute inside Docker. The CLI and native model client run on the host. Runtime dependencies are Python 3.12 and PyYAML; Docker supplies the Java tools. `docker/java/Dockerfile` provides a standalone JDK/Maven image. KTester generation also requires the existing KTester author tool and its container dependencies.

```sh
survival run /path/to/experiment --until scope
survival run /path/to/experiment --background
survival status /path/to/experiment
survival stop /path/to/experiment
survival show /path/to/experiment screen
survival run /path/to/experiment --stage survive --force
```

Stages run in this order: **prepare → scope → generate → screen → control → survive → cover → report**. Completed stages with matching configuration hashes are skipped. Hashes use the YAML configuration as written, before defaults are filled. Changes invalidate the affected stage and downstream stages. Interrupted work retains evidence for resumption; one process may run an experiment at a time. `--stage` requires current completed predecessors. `--until` stops after the selected stage. `--force` executes the selected work again. Forced generation archives the old group under `work/generation-history/`; `--stage generate --group NAME --force` regenerates just that group.

Developer groups can use project, name-targeted, or per-method coverage-scoped denominators. A derived group filters another Dev group’s survival records and measures its own coverage. `range.skip_untouched` skips irrelevant commits for every group while retaining indices. `survival.environment_errors` configures separate retries for matching failure messages. JUnit 4 and JUnit 5 are supported; generator Docker overrides let author tools use a different JDK.

Status and stage log lines show elapsed time, completed/total items, and an estimated finish based on that stage's throughput in the current attempt. Prepare counts builds (including the anchor), generate counts model calls, screen counts files, survive counts commits, and cover counts indices. Cached records count toward completion but not the current attempt's throughput. Estimates remain unknown until there is enough evidence; adaptive KTester call totals remain unknown until generation stops. `survival stop` terminates the recorded process (and its process group for detached runs), marks the running stage failed with `stopped by user`, and releases the run lock. Stage exceptions are saved in `state/<stage>-error.txt`, named in the failure log line.

Use `scope.rule: all-eligible` to select every class satisfying `min_methods` and `exclude_accessor_heavy`, without the `target_methods` or `max_classes` caps. Ranking still determines output order. If an anchor is off the endpoint's first-parent history, prepare names the first containing first-parent commit and reports whether its module tree matches the anchor.

Screening keeps methods that pass every configured anchor run and deduplicates exact whitespace-stripped bodies. Survival uses a fixed denominator: a lost method never returns. Build failures carry counts forward. Coverage uses the methods alive entering each index, on changed production commits and their predecessors. Reports include Markdown tables, an SVG figure (plus PNG when the JDK renderer succeeds), raw records, consistency checks, and a summary JSON file.

To replay archived generation without new model calls:

```sh
survival import /path/to/replay --from /path/to/reference --what generated
# Optional: use an archived stable developer baseline.
survival import /path/to/replay --from /path/to/reference --what dev-baseline
survival run /path/to/replay
```

Imports record copied-file hashes and synthesize round manifests from archived directories and funnels. API credentials are read privately from the configured environment file and are excluded from model-call records. A model call interrupted before its response was recorded requires ledger inspection before another call is issued.

Build a dashboard from an `experiments.yaml` list of `{path, title, layout}` entries. Supported layouts are `unified`, `per-group`, and `testpilot-ss`.

```sh
survival dashboard build --experiments experiments.yaml --out dashboard
pytest
```

The dashboard is a single offline `index.html` that works from `file://`. See [design](docs/design.md), [result formats](docs/result-formats.md), and the [reference replay configuration](examples/jsv-run2/experiment.yaml). JavaScript project/generator combinations are recognized but not implemented in this version.
