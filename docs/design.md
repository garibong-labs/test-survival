# Design

## Experiment model

An experiment takes a Java project, an anchor commit `t`, and a first-parent range ending at a fixed commit. Developer tests are frozen at `t`; configured generators produce additional test groups. Screening fixes each group's denominator. Survival compiles and runs those frozen methods across the range. A lost method never returns. Coverage measures the methods alive entering each relevant revision, and reports summarize survival and changed-line coverage.

The implementation supports Maven projects and JUnit 4 or JUnit 5. JavaScript combinations are reserved in the schema and rejected as unimplemented. The reference replay configuration is [examples/jsv-run2/experiment.yaml](../examples/jsv-run2/experiment.yaml).

## CLI

```sh
survival init  <exp-dir> --language java --build maven      # writes a commented experiment.yaml template
survival run   <exp-dir> [--until <stage>] [--stage <stage>] [--force] [--background] [--group <generator>]
survival show  <exp-dir> <stage>                            # prints the stage's summary table
survival status <exp-dir>                                   # per-stage status, denominators, survivors
survival import <exp-dir> --from <other-exp-dir> --what generated[,dev-baseline]
survival dashboard build --experiments <experiments.yaml> --out <dir>
```

Stage order: `prepare, scope, generate, screen, control, survive, cover, report`.

Resume rule. Each stage declares the configuration sections it depends on (table below). Before running a stage, compute the SHA-256 of the canonical JSON of those sections as the user wrote them, before defaults are filled in. Adding a new optional key to the schema must not change the hash of an existing `experiment.yaml` that does not use it; `tests/test_config.py` pins the per-stage hashes of `examples/jsv-run2/experiment.yaml` so this is caught. If `state/<stage>.json` says `completed` with the same hash, skip the stage. If the hash differs, run the stage and every later stage again; do not delete earlier stages' outputs. `--force` runs the named stage regardless of the hash. `--until <stage>` runs through that stage and stops. `--stage <stage>` runs only that stage (fails if earlier stages are not completed).

`--background` detaches the run, writes a PID and a log path to `state/run.json`, and returns. `status` reports whether that process is alive and shows the last log line. A second `run` while one is alive refuses to start.

`import` copies, from another experiment folder, either `generated/<group>/` for every group whose name exists in both configurations (`--what generated`) or `dev/dev-methods-all.csv` and `dev/dev-denominator.csv` (`--what dev-baseline`). It records the source path and the SHA-256 of every copied file in `state/import.json`, and marks the `generate` stage (or the Dev-baseline part of `prepare`) as completed for the destination configuration, retaining copied-file provenance. An archived source folder may not contain files that only this tool writes (for example `generated/<group>/rounds.json`); `import` synthesizes those from what is there: `rounds.json` is built from the `round<N>` and `round<N>-retry<k>` directories and their `funnel.json`, with `source: imported`. This is how the acceptance test replays an experiment without new model calls.

Every stage writes a short human-readable summary to stdout and appends it to `state/log.txt`.

## Configuration (`experiment.yaml`)

Full schema with defaults. Unknown keys are an error. `init` writes this with comments.

```yaml
name: string                    # required. Used in reports and the dashboard.

project:
  repo: path                    # host path of the project fork
  language: java                # java | javascript   (javascript is validated but not implemented yet)
  build: maven                  # maven | npm | pnpm   (npm/pnpm validated only)
  module: "."                   # module directory relative to repo (multi-module: e.g. ta4j-core)
  test_framework: auto          # auto | junit5 | junit4   (auto: junit4 when the module's test classpath has junit:junit and no jupiter engine)

anchor: string                  # tag or sha of t

range:
  branch: origin/master         # ref whose first-parent history is walked
  end: null                     # sha. If null, resolve branch at prepare time and record the sha in anchor.json.
  mode: first-parent            # only value
  limit: null                   # int. Keep only the first N commits after t.
  skip_untouched: none          # none | module | main-sources  (see Rules below)

scope:
  rule: change-ranked           # dedicated-test | change-ranked | explicit   (which classes are candidates)
  rank_by: change-count         # change-count | method-count   (how candidates are ordered; ignored for explicit)
  min_methods: 3                # class must have at least this many focal methods
  exclude_accessor_heavy: true  # drop classes where more than half of the methods are trivial accessors
  target_methods: [50, 80]      # change-ranked: stop adding classes once the focal method total is in this range
  max_classes: 10
  classes: []                   # explicit: fully qualified class names
  min_classes: 1                # stop if fewer classes are selected

dev:
  unmapped: error                # error | exclude; exclude unmappable baseline cases from every Dev group
  stable_runs: 3
  groups:                       # one or more developer groups; each becomes a column. Shorthand `dev: {denominator: project, stable_runs: 3}` means one group named `dev`.
    - name: dev
      denominator: project      # project | scope-targeted | coverage-scoped
    - name: dev-scope
      denominator: coverage-scoped
      derive_from: dev          # survival is derived from this group's raw records instead of re-running; coverage still runs

model:
  provider: openrouter
  name: openai/gpt-oss-120b
  env_file: ~/_projects/experiment-projects/KTester/docker/.env # optional; no default file, process environment takes precedence
  env_key: OPENROUTER_API_KEY

generators:                     # generator groups follow the configured developer groups
  - name: string
    method: ktester             # ouedraogo | ktester | testpilot
    impl: author-tool           # author-tool | native
    tool_path: path             # author-tool only
    params: {}                  # method-specific, see Generators below
    docker: null                # optional override of runner.docker for this generator only (e.g. the KTester compose on JDK 17 while the runner uses JDK 25)
    rounds:
      max: 1
      until_dev_matched: false  # stop rounds once kept methods >= the first dev group's denominator
      stop_after_zero_compile: 2 # after N consecutive zero-compile rounds, stop this group and record 0. 0 disables.
    context:
      dependency_signatures: {enabled: false, max_types: 15, max_lines: 400}
    prompt:
      api_constraint: false
    postprocess:
      import_repair: {enabled: false, passes: 2}

screen:
  stable_runs: 3
  dedup: exact-body             # only value
  matching: none                # none | subsample-to-dev

survival:
  definition: cumulative-fixed-denominator   # only value
  flaky_rerun: 1
  build_fail: carry-over        # only value
  environment_errors:           # failures caused by the execution environment, not by the code under test
    patterns: []                # substrings matched against the failure message, e.g. ["java.net.BindException"]
    max_reruns: 3
    backoff_seconds: 5

coverage:
  changed_line: true
  at_anchor: true
  groups: alive-entering        # only value

runner:
  docker:
    compose_files: []           # list of compose files, run via `docker compose -f ... run --rm <service>`
    service: ktester
    image: null                 # alternative to compose: a plain image run with `docker run --rm`, e.g. survival-java:25 built from docker/java/Dockerfile with --build-arg JDK_VERSION=25
    m2_volume: survival-m2       # image mode: named volume at /root/.m2; reused across containers
    mounts: {}                  # host path -> container path, used to translate paths in commands
    env_file: null              # passed to the container when image is used
  reuse:
    checkouts: null             # path to an existing checkouts dir (host path), used read-only to seed work/checkouts
    classpath_cache: null       # path to an existing classpath cache dir
  workers: {build: 4, survive: 4, cover: 4}
  timeouts: {build: 600, compile: 300, test_class: 120}
  jvm: {launcher: 1g, javac: 512m, jacoco: 1g}
  cache:
    classpath: pom-sha256       # pom-sha256 | none
    node_modules: lockfile-sha256
```

Validation errors name the key and the allowed values. The implemented combinations in this work are `language: java` + `build: maven` with `test_framework` junit5 or junit4, generators `ktester/author-tool` and `ouedraogo/native`. Any other combination fails validation with "not implemented in this version of the tool" and the key.

## Stages, outputs, hash inputs

All paths are relative to the experiment folder. File names are fixed and identical to the hand-run experiment so that the dashboard and the acceptance comparison work.

| Stage | Work | Outputs | Hash inputs |
|---|---|---|---|
| prepare | Resolve anchor and range end. Checkout `t` into `work/checkouts/0`, build. Copy the test sources at `t` to `dev/src-test-t/`. Run the Dev suite `dev.stable_runs` times, record per-method states. List first-parent commits `t..end`. For each commit: checkout into `work/checkouts/<index>`, compile production code with the classpath cache, record build status. | `results/anchor.json`, `results/commits.csv`, `results/commits-check.md`, `results/builds/<index>.json`, `results/builds/logs/`, `dev/dev-methods-all.csv` | project, anchor, range, dev.stable_runs, runner |
| scope | Survey production classes: focal method count, whether a dedicated test class exists, number of commits in range that change the class, accessor ratio. Apply `scope.rule` and `scope.rank_by`. Build each Dev group's denominator by its rule (`coverage-scoped` runs the per-method coverage pass). Build the KTester population file when a ktester group exists. | `population/class-survey.csv`, `population/accessor-audit.csv`, `population/scope.md`, `population/ktester-population.csv`, `results/scope-selection.json`, `dev/<group>-denominator.csv` (the first group keeps the historical name `dev/dev-denominator.csv`), `dev/<group>-excluded.csv`, `results/dev-method-coverage-t.csv`, `results/dev-method-coverage-t/` | + scope, dev, generators[].name/method |
| generate | For each generator group, run rounds under the stop rules. Archive everything. | `generated/<group>/round<N>/...` (see Generators below), `generated/<group>/rounds.json` | + model, that generator's entry |
| screen | For each group and round: compile, run `screen.stable_runs` times at `t`, keep methods passing every run, drop exact duplicate bodies (SHA-256 of the normalized method body). Apply `screen.matching`. Freeze denominators. | `results/llm-<group>-denominator.csv`, `generated/<group>/round<N>/funnel.md` and `funnel.json`, `results/frozen-denominators.json`, `results/denominators.json`, `results/<group>-round<N>-compile-failures.json` | + screen |
| control | Run every frozen method once more at `t`; all must pass. | `results/control-at-t.md`, `results/control-at-t.json` | (through screen) |
| survive | Phase A over all commits for all measured groups; derived Dev groups are filtered from their source group's records; skipped commits (`range.skip_untouched`) get placeholder records. | `results/raw/<index>-<group>.json`, `results/raw-derived/<index>-<group>.json`, `results/skipped-commits.csv`, `results/survival.csv`, `results/first-failures.csv` | + survival, range.skip_untouched |
| cover | Phase B at the coverage indices for all groups. | `results/changes/<index>.json`, `results/coverage/<index>-<group>.xml`, `results/coverage/<index>-<group>-lines.csv`, `results/raw-coverage/<index>-<group>.json`, `results/changed-lines.csv`, `results/coverage-commits.csv`, `results/alive-entering.json` | + coverage |
| report | Tables 1 to 4, coverage at t, figure, consistency, README section. | `results/table1-survival-by-event.md`, `table2-breaking-commits.md`, `table3-changed-line-coverage.md`, `table4-exposure-vs-break.md`, `coverage-t.md`, `figure1-survival-by-commit.svg` (+`.png`), `consistency.md`, `survival-consistency.json`, `coverage-consistency.json`, `final-summary.json` | (everything) |

## Generators

Common interface (`generators/base.py`): `generate(round_no) -> RoundResult` where `RoundResult` lists the produced test files, the model call records, and a funnel. Generators never screen; `screen` does. Author-tool wrappers preserve every file the tool produced, even when the tool crashed mid-way, and record the crash in `generated/<group>/round<N>/run.json`.

Round loop (`rules/stop.py`), applied by `stages/generate.py` using only the group's `rounds` configuration:

1. Run round N. Hand the files to a provisional screening (same code as `screen`) to count compiled files and kept methods; write `funnel.json`.
2. If `stop_after_zero_compile > 0` and the last `stop_after_zero_compile` rounds all compiled 0 files, stop this group, record `stopped: zero-compile` in `rounds.json`, and continue with the next group.
3. If `until_dev_matched` and the cumulative kept methods across rounds >= the first dev group's denominator, stop.
4. If N = `rounds.max`, stop.

Each generation attempt uses a new round number; zero-compile results do not retry the same round. The importer still recognizes archived `round<N>-retry<k>` directories as rounds of their own.

### `ktester` / `author-tool`

Tool: `~/_projects/experiment-projects/KTester` on branch `experiment/2026-10-week4-jsv-commit-survival` (contains the extractor patch for `NetworkXNoPath`). Record its HEAD sha as `ktester_base` in `anchor.json`. Do not copy the tool into this repository.

The wrapper ports `prepare_ktester.py` (dataset preparation from `population/ktester-population.csv`, `DatasetPrepare`, then deterministic id renumbering from `renumber_dataset_ids.py` into `rules/matching.py`), `generate_ktester.py` (params `fix_tries`, `temperature`, `workers`), and `record_ktester_completion.py`. Output layout under `generated/ktester/round1/`: `json-schema-validator/test_classes/*.java` (the tool's output tree, project name as the tool writes it), `raw-responses/`, `generation.log`, `run.json`, `time/`, `sources/` (flat copy of produced test files), `funnel.json`, `funnel.md`. The tool is invoked inside the container defined by the generator's `docker` override when present, otherwise `runner.docker`. The KTester tool needs JDK 17; when the runner image is a newer JDK (ta4j-core needs 25), the override points at the KTester compose.

### `ouedraogo` / `native`

Five prompt templates (`ZSL`, `FSL`, `CoT`, `ToT`, `GToT`) are packaged in `generators/parts/templates/` and preserve the archived jsoup templates. One model call per prompt type per scope class per round (5 classes × 5 types = 25 calls). Parameters `temperature`, `max_tokens`, `prompts` (list of enabled types). The native generator ports `generate_ouedraogo.py`, `tools/ouedraogo/generate.py`, `render_cut_prompts.py`, `extract_v2.py`, `extract_llm.py`, `dedup_lib.py`. Output layout under `generated/<group>/round<N>/`: `prompts/<Class>/<TYPE>.txt`, `raw-responses/`, `records/`, `runs/`, `sources/<Class>/<TYPE>/<TestClass>.java`, `validation/`, `validation-logs/`, `funnel.json`, `funnel.md`, `generation.json`. Every model call is recorded with request parameters, response metadata, and timing; never the key.

Three optional adjustments. All three are off by default and are what distinguishes the groups `ouedraogo-adj` and `ouedraogo-adj2` from `ouedraogo` in the acceptance configuration.

`prompt.api_constraint: true` appends these two sentences after the class source, identical for all five prompt types:

> Use only classes, constructors and methods that appear in the class source above or in the dependency signatures below. Do not use APIs from other versions of this library. Construct real objects through the constructors or factory methods shown instead of mocking; do not mock or subclass abstract classes or interfaces of this library.

`context.dependency_signatures.enabled: true` appends, after the constraint, one block per referenced project type: `// public API of <FQN>` followed by the type header with its declaration modifiers (`public abstract class ... extends ... implements ...`, `public interface ...`, `public enum ...`, nested `public static class ...`), constructors, and public/protected method signatures without bodies. Type parameters are printed only when the declaration has them. Varargs print as `Type...`. Referenced types are: imports under the project's base package (including nested types), same-package types used in field declarations, constructor parameters and method signatures, and the superclass chain. Order: superclass chain, constructor parameter types, field types, the rest. Cap at `max_types` types and `max_lines` lines; when the cap cuts, keep the earlier ones and record the omitted types in `prompts/<Class>/dependency-block.json`. Before any model call, render all blocks and fail the stage if any block contains `<>` with nothing inside or a malformed type header (package-private types legitimately have no access modifier) (this is the pre-call gate of the hand-run addendum). The Ouédraogo context helper lives in `generators/parts/java/DependencyInventory2.java`; the legacy copy in `adapters/java` remains unchanged because its bytes are part of completed runner provenance. The context helper uses the runner's JDK (17+) syntax parser, preserves record components and sealed `permits` clauses, and falls back to the AST for synthetic parameter positions. An unrenderable type produces an `S` row (FQN, source file, reason) in `source-api-inventory.tsv` and an entry in `skipped-types.json`, with no partial type/API rows. Parser errors also produce `D` diagnostic rows; other types remain available.

`postprocess.import_repair.enabled: true` runs after extraction and before screening, without model calls: compile each extracted file once; for every `cannot find symbol ... class X` where `X` resolves to exactly one type in the `t` source inventory (top-level or nested) or to exactly one type in `org.junit`, `org.junit.jupiter.api`, `org.mockito`, `org.mockito.stubbing`, `org.mockito.invocation`, `org.hamcrest`, add the import and compile again; at most `passes` passes; change nothing but import lines. Keep the pre-repair source as `<file>.pre-repair` beside the repaired file and record every added import per file in `round<N>/import-fixes.json`.

Compile-failure classification (`results/<group>-round<N>-compile-failures.json`): for every file that failed to compile, categorize the first diagnostic into `missing-import`, `signature-mismatch`, `unknown-api`, `syntax`, `other`, following `audit_run2.py`'s categories where it has them.

## Rules

| Rule | Values | Port from |
|---|---|---|
| `scope.rule` | Candidate set. `dedicated-test`: eligible classes that have a dedicated test class. `change-ranked`: all eligible classes (≥ `min_methods`, not accessor-heavy when enabled). `explicit`: `scope.classes` as given, no ranking. | `select_scope.py` (dedicated-test), `select_scope_run2.py` (change-ranked) |
| `scope.rank_by` | Order of the candidates. `change-count`: number of commits in range that change the class, descending (default; this is what both historical scripts did). `method-count`: focal method count, descending. Ties break by fully qualified class name. Candidates are added in that order until the focal method total lies in `target_methods` or `max_classes` is reached. | same files |
| `dev.groups[].denominator` | `project`: every stable Dev method of the module. `scope-targeted`: stable Dev methods in test classes that target a scope class (projects with one test class per production class). `coverage-scoped`: stable Dev methods that, run alone at `t` under JaCoCo, cover at least one executable line of a scope file (projects with issue-based or suite tests). For `coverage-scoped`, `scope` runs each method of the `project` set alone with the JaCoCo agent (same selectors, caps and timeouts as `cover`, four workers) and writes `results/dev-method-coverage-t/<class>#<method>.xml`, `results/dev-method-coverage-t.csv` (`class,method,scope_lines_covered,scope_files,total_lines_covered,seconds,status`), `dev/<group>-denominator.csv` (`class,method,kind`) and `dev/<group>-excluded.csv`. A method that fails alone is excluded and counted in the stage summary. | `baseline.py`, `select_scope_run2.py`, reference addendum 4 (`results/dev-method-coverage-t.csv`, `dev/dev-scope-denominator.csv` once that addendum has run) |
| `dev.groups[].derive_from` | When set, `survive` does not run this group: it filters the named group's raw records to this group's denominator and writes `results/raw-derived/<index>-<group>.json` (same schema, plus `derived_from`). `cover` runs this group normally, because alive-entering sets differ. The denominator must be a subset of the named group's. `consistency.md` counts measured and derived records separately. | reference addendum 4 |
| `range.skip_untouched` | `none`: evaluate every commit. `module`: a commit whose `git diff --name-only <parent> <sha>` lists no path under `<project.module>/` and no file named `pom.xml` is not evaluated for any group; it keeps the previous counts (`evaluated: no` in `survival.csv`), is excluded from the coverage indices, and keeps its index. `main-sources`: the same with `<project.module>/src/main/` instead of the whole module directory, for single-module projects where documentation-only or test-only commits are common. Skipped commits are listed in `results/skipped-commits.csv` (`index,sha,reason`) and get a raw record `{index, sha, group, status: skipped-untouched}` per group. Frozen tests and their fixtures can only react to production code and build files, so the skip applies to every group alike. | new (needed for langchain4j-core: 3,594 commits after 0.28.0, 162 touch the core module) |
| `project.test_framework` | `junit5`: uses the existing JUnit Platform discovery and selectors. `junit4`: test methods are `@org.junit.Test` methods (kind `plain`; a class with `@RunWith(Parameterized.class)` gives kind `parameterized`; `@Ignore` methods are not enumerated); the launcher adds `junit-vintage-engine` and the JUnit 4 jar to the test classpath; method selectors use `class#method` as before. `auto` picks by the module's test dependencies. ta4j-core (t = 0.16, 647 Dev methods) is JUnit 4. | new |
| `survival.definition` | fixed denominator per group; cumulative; non-increasing; build-failed commits are not evaluated and carry counts forward; `fail`/`error` get one rerun, a passing rerun keeps the method alive and marks it `flaky`; a compile failure of a test file loses every denominator method in that file; a method absent from results is `absent`; first-failure kind recorded once | `run_commit.py`, `aggregate_survival.py` |
| `survival.environment_errors` | When a method's failure message contains one of `patterns`, the failure is treated as environmental: wait `backoff_seconds`, rerun that test class, up to `max_reruns` times, separately from `flaky_rerun`. A pass during these reruns keeps the method alive and records `environment_reruns: N` on the method (not `flaky`). If every rerun fails, record the last state as the first failure. `first-failures.csv` gains no column; per-group totals of environment reruns go into `final-summary.json`. Empty `patterns` disables the feature and leaves behavior unchanged. | new |
| `screen.matching` | `none`; `subsample-to-dev`: when a group's kept count exceeds the Dev denominator, keep a deterministic subsample (seeded by the group name) of Dev-denominator size | `validate_round.py` |
| id renumbering | deterministic renumbering of KTester dataset ids when overload suffixes collide | `renumber_dataset_ids.py` |

Each rule is a pure function with unit tests in `tests/`.

## Execution and provenance

Java runs inside the configured Docker environment; the native model client runs on the host. A generator's Docker override is a complete replacement of `runner.docker` for that generator, with the same validation and defaults. Stage hashes use the configuration as written. Raw measurement hashes additionally bind resolved execution settings, source bytes, project settings, and runner components.

Dev groups retain their configured order before generator groups. `derive_from` must name another Dev group, dependencies must be acyclic, and scope/screen validate denominator subsets. Derived raw files retain source launch evidence while filtering methods and requested IDs. Measured and derived record counts are reported separately.

Skipped commits retain indices and produce placeholders for every group. Coverage excludes skipped indices. When a required predecessor was skipped, `coverage-index-aliases.json` maps it to the preceding evaluated revision with the same production tree; the pre-image measurement uses that revision. Environmental retry totals count method attempts; derived groups inherit their source observations.

JUnit 4 fixture tests cover ordinary and parameterized methods, Ignore, Vintage execution, environment retry recovery, and per-method JaCoCo XML. JaCoCo 0.8.12 remains the choice for existing class files through Java 22; newer class files select 0.8.14, which [officially supports Java 25](https://www.jacoco.org/jacoco/trunk/doc/changes.html). Jar hashes are pinned in the package manifest.
