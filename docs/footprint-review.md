# Experiment footprint review — Part 1

Review baseline: `01ae5d006bd4e900fe3e708eee28895fed9d4276` (`main`).
[Archive PR #30](https://github.com/garibong-labs/test-survival/pull/30) merged on
2026-11-04 at 14:02:26 UTC. `feat/footprint` was created after fetching and
fast-forward checking `main`. This document is the Part 1 deliverable; it does
not implement cleanup or authorize Part 2.

## Findings and priorities

1. **Bound the lifetime of complete source/build trees first.** `prepare` eagerly
   builds the entire range. Merely deleting after coverage would still retain
   the peak of every checkout throughout preparation and survival. Release each
   successful nonzero prepare checkout after its build record is durable, then
   reconstruct only indices needed by active survival/coverage jobs. Incremental
   release needs the failure-retention agreement below; the strictly conservative
   default releases these trees only after the whole prepare stage succeeds. Release
   them again after all consumers of that index finish. Index zero is shared by
   worker launch, setup, generation, and even PNG rendering: keep it through
   successful report completion. Recovery must work when an earlier stage was
   skipped because its completion record survived but its checkout did not.
2. **Pool scratch is larger than the top-level checkouts in this snapshot.** The
   nine active J pools contain 381,568 files / 14,079,195,141 bytes, of which
   359,816 files / 13,564,189,148 bytes are under their `work/` trees. In particular,
   pool `work/policy-coverage` is 51,465 files / 11,821,944,757 bytes, and
   `work/dev-screen` is 268,533 files / 1,149,529,356 bytes. Verified pool publication
   is a particularly valuable cleanup boundary. Preserve pool source snapshots,
   `pool.json`, all `generated/`, and result evidence.
3. **Do not recursively delete every directory named `work`.** Force histories
   contain previous results and possibly paid model output. KTester writes a
   completion artifact under `work/ktester`; the compiler cache is explicitly
   protected. Ownership and failure state, rather than a directory basename,
   must decide eligibility.
4. **Compression reduces bytes, not file count.** Group coverage lines are needed
   by reports and policy coverage. Raw evidence is needed by resume, imports,
   consistency checks, and policies. Keep these logical artifacts and compress
   them. Eliding successful `dev-scope.xml` after durable CSV publication is
   possible, subject to the comparison-contract decision below.
5. **Do not add shared writable checkouts.** J has 236 checkout markers for 227
   unique SHAs: only nine redundant copies (3.8%). They are the nine pool anchors,
   totaling 60,387,014 bytes including their markers, about 3.6% of measured
   checkout bytes. Eager lifetime reduction matters much more. Maven, tests, and
   KTester mutate build trees; hard-linking them risks changing another job's
   inputs. Defer SHA sharing. An immutable source cache plus private build trees
   would need independent isolation tests and is not a cheap requirement here.

## Measurement method and scope

Read-only traversal on **2026-11-05, 04:28:57–04:29:54 UTC** (13:28:57–13:29:54
Asia/Seoul), using `os.scandir` and `stat(follow_symlinks=False)`. No experiment
command, Maven build, model call, archive, compression, file rewrite, or deletion
was run against these experiments. Metadata, saved run status, checkout markers,
and compile-event JSON were read to classify the measurements. All three saved
run records and their final report records say `completed`; this is a point-in-time
scan, not a filesystem snapshot or an independent process-liveness attestation.
There were no traversal errors and no symlinks. Byte counts are exact logical
`st_size`, not rounded `du` output. Allocated bytes are the sum of `st_blocks * 512`;
they do not prove physical reclaimable space on APFS, with clones or snapshots.
Directories are counted separately; files include hidden files and empty locks.

All roots below are under `~/_projects/experiment-projects/`:

| Key | Experiment | Files | Logical bytes | GiB | Allocated bytes | Directories |
|---|---|---:|---:|---:|---:|---:|
| T | `ta4j/experiments/commit-survival` | 17,828 | 8,598,089,917 | 8.008 | 8,645,406,720 | 1,404 |
| J | `json-schema-validator/experiments/regen-policies` | 1,045,976 | 18,230,835,259 | 16.979 | 20,974,272,512 | 146,785 |
| D | `dnsjava/experiments/commit-survival` | 74,477 | 4,247,106,979 | 3.955 | 4,429,467,648 | 21,120 |

T and D have no `work/` tree now. J's top-level `work/checkouts` is 352,684 files /
1,623,989,963 bytes, `work/policy-survive` is 199,765 / 1,434,724,327, and
`work/phase-A` is 80,379 / 292,475,213. These differ from the supplied November 4
counts; this review does not infer why. The complete `results/policy/pools`
subtree is 381,572 / 14,079,219,733 including four structural `.DS_Store` files
above the nine pool roots. T's `results/coverage` (2,544 / 5,618,069,858), T's
`results/raw` (1,587 / 2,470,923,185), D's `results/compile-events`
(60,829 / 265,003,234), and D's `results/dev-method-coverage-t`
(3,750 / 2,582,400,083) reproduce the supplied counts.

**`work/compile-cache` measures 0 files / 0 bytes in all three roots and all nine
active pools.** This is absence in these snapshots, not permission to remove it
from future runs. No hypothetical cache saving is included.

## Path ownership, readers, and retention

The following catalog partitions the files by ownership and lifecycle. Paths are
relative to an experiment root. **Every row also applies beneath
`results/policy/pools/<group>/<index>/`**; the measurement table separates these
nested experiments to avoid double counting. Pool-only root files are row 49.
`**` includes arbitrary descendants, including dependency/build outputs created
by Maven and author tools. Rows with more specific patterns take precedence;
row 67 explicitly accounts for OS/user/legacy additions rather than treating
them as tool-owned scratch. `generated/` remains protected even when it contains
classes, caches, validation records, or `.DS_Store`.

“Last reader” means last automatic consumer in an ordinary successful pipeline.
Reports, imports, manual audits, and subsequent policy runs can read durable
results indefinitely. “Regenerable” does not mean byte-identical replay of an
observation; measurements and failure diagnostics are retained even if their
execution could theoretically be repeated.

Change codes used in both tables:

- **K / P / CACHE:** unchanged file count and bytes (`F`, `B`). P is protected
  generated/history evidence; CACHE is the explicit Part 2.2 exception.
- **C1:** Part 2.1 checkout cleanup. Expected successful final count/bytes `0 / 0`.
  Index zero survives until full completion; failed/interrupted owners retain
  their trees. A nonzero index is released between stages and rebuilt on demand.
- **C2 / C2END:** Part 2.2 scratch cleanup. Expected `0 / 0` after owning-stage /
  whole-run success, **subject to retained failure evidence**. Validation logs
  need the later screening boundary described below. `--keep-work` retains F/B.
- **KT:** Part 2.2 conditional KTester scratch, expected `0 / 0` only after
  verifying archived model output and retaining the report completion record.
- **G16 / G17 / G39 / G14:** Part 2.4; count stays F, estimated bytes are respectively
  `ceil(B/16)`, `ceil(B/17)`, `ceil(B/39)`, `ceil(B/14)`. These are planning estimates
  using the user's sample ratios, **not measured compressed sizes**; G39 on
  raw-derived/raw-coverage is an extrapolation. Compression overhead can dominate
  tiny records. Original logical content hashes must remain equal.
- **DROPXML:** Part 2.4; expected `0 / 0` for new successful dev-scope group XML
  after its complete lines CSV is published. Keep old XML readable. Retain XML
  on failure; per-method XML is G14, not DROPXML.
- **EVENT:** Part 2.3; one gzip stream and one stable lock per index/stage context,
  full failed diagnostics inline, so `2 × contexts` files. Expected byte budget
  is **at most current B as a design target**, not a proven compression bound.
  Exact gzip sizes require serializing the new schema and are deferred to Part 2.
  Preserve starts, finishes, interruptions, and JDK-probe evidence.

<!-- ownership-table -->
| ID | Path pattern | Writer | Later readers; last reader | Resume dependency | Regenerable from git / earlier results? | Change |
|---|---|---|---|---|---|---|
| 01 | `experiment.yaml` | init / user | all stages, status, show, dashboard; indefinite | Yes: configuration | No: user input | K |
| 02 | `README.md` | report | human / export; report | No | Yes: report summaries | K |
| 03 | `ARCHIVED.json` | archive --prune | status; until restoration | Yes: archived status | No: archive identity | K |
| 04 | `state/{stage,policy-step,resolved-range,run,run-owner,import,container-stage,background,stopping}.json` | pipeline, policy Steps, import, lifecycle | resume, progress/status, archive; indefinite | Yes: completion, provenance, stop barrier | No: original lifecycle/import observations | K |
| 05 | `state/{run,stop,...}.lock` | run/stop/archive | all coordinators; indefinite | Yes: lock inode | No: never unlink published locks | K |
| 06 | `state/{workers,containers}/**` | lifecycle registration | stop, ownership checks; run/stop complete | Yes: interrupted process cleanup | No: process identities | K |
| 07 | `state/*.{txt,log}` | pipeline, policy, Docker | diagnostics / progress; indefinite audit | Errors: yes; logs: audit | No: historical failure/log evidence | K |
| 08 | `dev/src-test-t/**` | prepare, import, pool snapshot | scope, control, survive, cover, policy snapshots; policy execution | Yes: frozen sources/resources | Git source yes; frozen/imported identity must be preserved | K |
| 09 | `dev/*-denominator.csv` | scope / import / pool target | generate, screen, reports, policy; report/import | Yes | Yes from preserved selection evidence | K |
| 10 | `dev/{dev-methods-all,*-excluded}.csv` | prepare, scope, import | scope, screen, imports, reports; report/import | Yes | Only from retained baseline/selection evidence | K |
| 11 | `population/{class-survey,accessor-audit,methods,ktester-population}.csv; accessor-methods.tsv` | scope / pool prepare | scope selection, generators, policy unit mapping; policy execution | Yes | Yes: git + frozen baseline + config | K |
| 12 | `population/{scope.json,scope.md}` | scope / pool prepare | all downstream stages, policy/report; policy report | Yes | Yes: scope inputs | K |
| 13 | `population/dataset_{info,info.raw,meta}.json` | KTester dataset | generation, audit; generation / audit | Yes for KTester | Yes: fixed tool/git/scope; preserve metadata | K |
| 14 | `generated/<group>/<round>/sources/**; */test_classes/**` | generate / import | screen, control, survive, cover, policy snapshots; indefinite | Yes | No: paid model output | P |
| 15 | `generated/<group>/<round>/validation/**` | screen_file | screen/funnel, policy exclusions; policy execution | Yes | Only by remeasurement; preserve | P |
| 16 | `generated/<group>/<round>/prompts/**` | Ouédraogo context/prompts | model-call resume, import repair; generate / audit | Yes | Some from git; protected as model evidence | P |
| 17 | `generated/<group>/{rounds.json,<round>/**} (remaining records)` | native/KTester generate, screening, import | round resume, screen, reports, policy; indefinite | Yes | No: requests, responses, usage, run, records, generation, funnel, repairs | P |
| 18 | `results/{anchor.json,commits.csv,commit-paths.json,commits-check.md}` | prepare / policy plan | all downstream, report/dashboard/policy; indefinite | Yes | Git range yes; anchor runtime metadata no | K |
| 19 | `results/builds/<index>.json` | prepare/build / policy build | survive, cover, runner classpath, policy; execution/report | Yes: authoritative build verdict | No: original build observation | K |
| 20 | `results/builds/logs/**` | Maven compile/dependency setup | build/resume diagnostics; build / audit | Failures: yes | No: command evidence | K |
| 21 | `results/baseline-reports/run<N>/**` | prepare Maven test copy | baseline resume; prepare | Yes until baseline fixed | Only rerunning tests; preserve | K |
| 22 | `results/baseline-{run*,unmapped}.*` | prepare | baseline reconstruction / audit; prepare/import | Yes | Only rerunning tests; preserve | K |
| 23 | `results/source-inventory{,-main-<i>,-test}.{json,tsv}; test-framework.json` | prepare / pool inventories / framework resolution | scope, generation, baseline/pool selection; generate/pool | Yes | Yes: git/frozen source + compiler | K |
| 24 | `results/runner-{setup,dependency-*}.{json,txt}` | setup_runner | setup resume, jar verification; last Java job | Yes | Jars downloadable; original attempt evidence no | K |
| 25 | `results/{scope-selection,dev-group-selection}.json; llm-*-denominator.csv; {frozen-denominators,denominators}.json` | scope/screen | control, survive, cover, report, policy, dashboard; indefinite | Yes | Yes: preserved screening evidence | K |
| 26 | `results/<group>-<round>-compile-failures.json; ktester-{prepare-*,snapshot,index,population-validation,round*}.*` | generate/screen/KTester | funnel diagnostics, policy exclusions, audit; policy/audit | Yes for exclusions | No: original diagnostics | K |
| 27 | `results/control/<i>-<group>.json; control-at-t.{json,md}` | control | resume, dev-results import, show; control/import | Yes | Only rerun tests; preserve | K |
| 28 | `results/raw/<i>-<group>.json` | survive / legacy policy | aggregate, reuse, report, imports, policy; indefinite | Yes | No: original measurements | G39 |
| 29 | `results/raw-derived/<i>-<group>.json` | derived survive | aggregate, imports, policy; indefinite | Yes | Yes: source raw + frozen subset | G39 |
| 30 | `results/raw-coverage/<i>-<group>.json` | cover | reuse, consistency, imports; report/import | Yes | No: original measurements | G39 |
| 31 | `results/coverage/<i>-<group>.xml (excluding dev-scope)` | JaCoCo worker | worker XML-to-lines conversion; same job (imports retain evidence) | No for group reuse; yes as evidence | Only from execution data; preserve | G16 |
| 32 | `results/coverage/<i>-dev-scope.xml` | JaCoCo worker | worker XML-to-lines; same job; imported as optional evidence | No after durable lines CSV | Only from execution data | DROPXML |
| 33 | `results/coverage/<i>-<group>-lines.csv` | JaCoCo worker | cover aggregate, reports, imports, policy cover; indefinite | Yes | From retained XML; otherwise rerun | G17 |
| 34 | `results/dev-method-coverage-t/*.{json,xml,csv}` | scope per-method worker | method cache (XML existence), scope selection, audit; scope | Yes | Only rerun methods | G14 |
| 35 | `results/dev-method-coverage-t.csv` | scope | denominator selection/report export; scope/export | Yes | Yes: preserved per-method data | K |
| 36 | `results/changes/<i>.json` | cover | aggregate, policy coverage; policy cover | Yes | Git diff + preserved executable line universe | K |
| 37 | `results/{survival,first-failures,skipped-commits,coverage-commits,changed-lines}.csv; alive-entering.json` | survive/cover | report, dashboard, policy, exports; indefinite | Yes | Yes: retained raw/build/frozen/coverage | K |
| 38 | `results/{survival-consistency,coverage-consistency,coverage-index-aliases,coverage-universe-mismatches,missing-survival-records}.json` | survive/cover/report | reports, status diagnostics; report | Yes | Yes from retained measurements | K |
| 39 | `results/{table*.md,coverage-t.md,consistency.md,final-summary.json}` | report | show, dashboard/export, KTester completion; indefinite | Summary used by completion | Yes except runtime timestamps/durations | K |
| 40 | `results/figure1-survival-by-commit.{svg,png}; figure-render.{json,log}` | report renderer | README/report/dashboard consumers; report | No | Yes with same renderer | K |
| 41 | `results/quarantine/**` | survive killed-worker guard | failure diagnosis; indefinite | Yes: failure evidence | No | K |
| 42 | `results/compile-events/<uuid>/**` | CompilationService | cache origin references / audit; indefinite | Starts needed to identify interruption | No: execution history | EVENT |
| 43 | `results/policy/{plan.json,plan.md,active-plan.json,needed-runs.json,excluded-sources.json}` | policy plan/execute | Steps, pools, survival, status/report; policy report | Yes | Yes from baseline and policy inputs | K |
| 44 | `results/policy/baseline-sources/**` | policy snapshot | policy compile/coverage, hash verification; policy survive | Yes | From preserved baseline sources; keep snapshot | K |
| 45 | `results/policy/batches/<i>[-regenerate].json` | policy worker | split into runs, failure/audit; policy survive | Yes for evidence; runs own reuse | No: original combined measurements | K |
| 46 | `results/policy/runs/<execution-hash>/<i>.json` | policy Runs | resume, segments, cover, report/progress; policy report | Yes | No: measurements | K |
| 47 | `results/policy/<policy>/{segments,alive,coverage-total}.json; {survival,first-failures,coverage-commits,coverage-missing}.csv; table1-*.md` | policy execute/cover/report | policy cover/report, export; indefinite | Yes | Yes from pools + runs + baseline | K |
| 48 | `results/policy/{summary.csv,summary.md,consistency.md,survival-by-commit.svg}` | policy report | human/export; indefinite | No | Yes from retained policy outputs | K |
| 49 | `pool.json; funnel.json; per-test-coverage.json` | pool build/snapshot | pool skip, policy selection/survive/cover; policy report | Yes: completion and frozen pool identity | No recomputation of completed pools | K |
| 50 | `results/{dev-screen.json,policy-coverage/<label>.json}` | pool screening / coverage | pool selection/snapshot; pool publication, then audit | Yes until pool verified | No: original measurements | K |
| 51 | `work/checkouts/0/**` | checkout + in-place Maven build | prepare, scope, generate, screen, control, survive, cover, report renderer; full run | Yes today; recreate on demand | Yes: git + build dependencies | C1 |
| 52 | `work/checkouts/<i>/** (i > 0)` | prepare.build / policy build | survive/cover, policy window + regenerate; last job for index | Yes today; recreate on demand | Yes, preserve original build evidence | C1 |
| 53 | `work/checkouts/<i>.json` | checkout | checkout validity; last checkout consumer | Rebuild if absent | Yes: git SHA | C1 |
| 54 | `work/compile-cache/**` | CompilationService | all compile consumers and future runs; indefinite | Optimization; not correctness | Yes, expensive; explicitly retained | CACHE |
| 55 | `work/phase-{A,B,control}/**` | run_group workers | worker parse/coverage + coordinator validation; owning stage success | Failed/interrupted: keep | Classes yes; diagnostics no | C2 |
| 56 | `work/policy-survive/<i>[-regenerate]{/**,-job.json,-job.log}` | policy worker | batch split, per-digest records, policy step; policy survive success | Failed/interrupted: keep | Classes yes; diagnostics no | C2 |
| 57 | `work/policy-coverage/**; work/dev-screen{/**,-job.*}` | pool workers | pool selection, verified snapshot; pool success | Failed/interrupted: keep | Scratch yes; original diagnostics no | C2 |
| 58 | `work/dev-method-coverage-t/**` | scope workers | per-method coverage + denominator; scope success | Failed/interrupted: keep | Scratch yes; original diagnostics no | C2 |
| 59 | `work/validation/<group>/<round>/**` | screen_file | screen_round failure classification; generate AND later screen | Yes: cached validation logs | Classes yes; failed diagnostics no | C2 |
| 60 | `work/import-repair/**; work/compile/<i>/**` | repair / compile_tests | repair/screen; owning stage success | Failed/interrupted: keep | Classes yes; diagnostics no | C2 |
| 61 | `work/classpaths/*.txt; work/classpath-jobs/*` | ClasspathCache/worker | build, framework detection, execution; last Java job | Yes today; regenerate | Yes: POM + dependencies | C2END |
| 62 | `work/libs/**; work/runtime/**; work/runner-setup-job.{json,log}` | setup_runner | all Java workers; last Java job | Yes today; setup restores | Jars/resources yes; job diagnostics no | C2END |
| 63 | `work/ktester/<group>/experiment-result.json` | report completion | human/audit; indefinite | No | Contains completion provenance; retain | K |
| 64 | `work/ktester/<group>/**; work/{ktester-baseline,utgen,chatunitest}/**` | KTester prepare/index/dataset/generate | later rounds, report completion; report | Yes through generation/report | Index/classes yes; any unarchived model output no | KT |
| 65 | `work/{generation-history,policy-history}/**` | --force archive/move | manual restore/audit; indefinite | History must survive cleanup | No: may contain paid generated outputs | P |
| 66 | `*.tmp / atomic publication leftovers` | atomic writers / interrupted writes | diagnose interrupted publication; resume | Keep on failure; ignored by readers | Partial evidence no | K |
| 67 | `external/legacy additions (.DS_Store, docs, prior configs, unknown)` | OS / user / older tools, not a cleanup ownership claim | human/legacy; indefinite | Unknown: preserve | Unknown | K |

Additional configurable output: `survival dashboard build --out DIR` writes
`DIR/index.html` (writer: dashboard; reader: browser; no resume dependency;
regenerable from saved summaries; K). It can be placed inside an experiment.
No dashboard output exists inside these three roots: **T/J/D/pools = 0 / 0**.
Archives and their index/sidecars are deliberately outside EXP_DIR (enforced by
`archive.py`); the in-experiment archive marker is row 03. Temporary archive ZIPs
are beside the repository, not inside an experiment. Maven caches mounted at
`/root/.m2` and Docker image storage are also outside this inventory.

## Exact per-pattern footprint

Each cell is **files / logical bytes**, before any proposed change. T, J and D
columns exclude active nested pools; JP sums the nine J pools. T and D have no
nested pools, so every corresponding pool-pattern measurement is `0 / 0`.
Rows are disjoint and their totals reconcile to the root scan, including
protected histories and OS files. Row 67 lists only `.DS_Store`, T’s prior YAML,
and J’s manually written report; there are no unidentified production artifacts.

| ID | T | J (excluding active pools) | D | JP (active pools) |
|---|---:|---:|---:|---:|
| 01 | 1 / 3,485 | 1 / 4,106 | 1 / 3,711 | 0 / 0 |
| 02 | 1 / 458 | 0 / 0 | 1 / 568 | 0 / 0 |
| 03 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 04 | 12 / 3,512 | 8,498 / 3,662,205 | 13 / 3,848 | 9 / 1,467 |
| 05 | 2 / 0 | 2 / 0 | 1 / 0 | 0 / 0 |
| 06 | 3,129 / 338,773 | 3,276 / 351,948 | 3,441 / 373,121 | 0 / 0 |
| 07 | 3 / 457,917 | 1 / 18,264,428 | 6 / 252,354 | 6 / 147,481 |
| 08 | 242 / 2,162,048 | 0 / 0 | 475 / 10,535,267 | 8,021 / 33,202,022 |
| 09 | 2 / 56,580 | 0 / 0 | 2 / 95,753 | 6 / 3,798 |
| 10 | 3 / 125,908 | 0 / 0 | 4 / 189,095 | 0 / 0 |
| 11 | 5 / 132,063 | 0 / 0 | 5 / 91,220 | 12 / 172,858 |
| 12 | 2 / 1,929 | 0 / 0 | 2 / 1,042 | 9 / 13,848 |
| 13 | 3 / 461,153 | 0 / 0 | 3 / 290,203 | 9 / 3,941,279 |
| 14 | 754 / 7,960,211 | 0 / 0 | 242 / 2,871,913 | 1,290 / 17,131,852 |
| 15 | 134 / 4,755,549 | 0 / 0 | 84 / 3,275,303 | 414 / 21,804,773 |
| 16 | 86 / 2,122,252 | 0 / 0 | 56 / 2,341,418 | 130 / 5,058,266 |
| 17 | 4,522 / 48,030,820 | 0 / 0 | 1,951 / 16,010,216 | 11,662 / 140,537,114 |
| 18 | 4 / 855,907 | 2 / 215,678 | 4 / 76,428 | 9 / 855 |
| 19 | 531 / 422,310 | 227 / 232,775 | 179 / 161,925 | 9 / 17,112 |
| 20 | 779 / 1,007,271 | 607 / 177,427 | 424 / 178,174 | 48 / 13,363 |
| 21 | 1,278 / 4,817,622 | 0 / 0 | 984 / 5,956,910 | 0 / 0 |
| 22 | 10 / 11,507 | 0 / 0 | 8 / 2,966,262 | 0 / 0 |
| 23 | 3 / 1,206,383 | 0 / 0 | 3 / 1,586,277 | 27 / 10,223,269 |
| 24 | 64 / 64,873 | 1 / 6,144 | 1 / 6,144 | 17 / 59,028 |
| 25 | 6 / 354,015 | 0 / 0 | 6 / 705,205 | 0 / 0 |
| 26 | 9 / 228,637 | 0 / 0 | 9 / 84,353 | 21 / 1,016,868 |
| 27 | 6 / 8,982,724 | 0 / 0 | 6 / 7,572,046 | 0 / 0 |
| 28 | 1,587 / 2,470,923,185 | 96 / 3,766,804 | 525 / 221,845,588 | 0 / 0 |
| 29 | 529 / 227,726,025 | 0 / 0 | 0 / 0 | 0 / 0 |
| 30 | 1,272 / 179,784,160 | 0 / 0 | 435 / 178,342,623 | 0 / 0 |
| 31 | 954 / 2,707,372,566 | 0 / 0 | 290 / 413,710,098 | 0 / 0 |
| 32 | 318 / 902,248,567 | 0 / 0 | 145 / 206,938,674 | 0 / 0 |
| 33 | 1,272 / 2,008,448,725 | 0 / 0 | 435 / 311,470,896 | 0 / 0 |
| 34 | 0 / 0 | 0 / 0 | 3,750 / 2,582,400,083 | 0 / 0 |
| 35 | 0 / 0 | 0 / 0 | 1 / 108,241 | 0 / 0 |
| 36 | 278 / 11,781,010 | 0 / 0 | 130 / 761,943 | 0 / 0 |
| 37 | 6 / 4,974,903 | 1 / 17 | 6 / 10,745,453 | 0 / 0 |
| 38 | 4 / 7,871 | 0 / 0 | 4 / 3,879 | 0 / 0 |
| 39 | 7 / 93,833 | 0 / 0 | 7 / 40,992 | 0 / 0 |
| 40 | 4 / 131,717 | 0 / 0 | 4 / 75,779 | 0 / 0 |
| 41 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 42 | 0 / 0 | 300 / 3,337,723 | 60,829 / 265,003,234 | 9 / 158,828 |
| 43 | 0 / 0 | 5 / 969,646 | 0 / 0 | 0 / 0 |
| 44 | 0 / 0 | 1,222 / 7,854,134 | 0 / 0 | 0 / 0 |
| 45 | 0 / 0 | 229 / 118,034,934 | 0 / 0 | 0 / 0 |
| 46 | 0 / 0 | 8,483 / 402,969,357 | 0 / 0 | 0 / 0 |
| 47 | 0 / 0 | 30 / 133,903,202 | 0 / 0 | 0 / 0 |
| 48 | 0 / 0 | 4 / 100,645 | 0 / 0 | 0 / 0 |
| 49 | 0 / 0 | 0 / 0 | 0 / 0 | 21 / 180,506,259 |
| 50 | 0 / 0 | 0 / 0 | 0 / 0 | 6 / 100,891,137 |
| 51 | 0 / 0 | 1,220 / 4,996,681 | 0 / 0 | 13,395 / 60,386,303 |
| 52 | 0 / 0 | 351,236 / 1,618,918,949 | 0 / 0 | 0 / 0 |
| 53 | 0 / 0 | 227 / 41,561 | 0 / 0 | 9 / 711 |
| 54 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 55 | 0 / 0 | 80,378 / 292,469,065 | 0 / 0 | 0 / 0 |
| 56 | 0 / 0 | 199,764 / 1,434,701,795 | 0 / 0 | 0 / 0 |
| 57 | 0 / 0 | 0 / 0 | 0 / 0 | 320,004 / 12,972,061,650 |
| 58 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 59 | 0 / 0 | 0 / 0 | 0 / 0 | 2,960 / 30,442,793 |
| 60 | 0 / 0 | 0 / 0 | 0 / 0 | 9 / 218,148 |
| 61 | 0 / 0 | 602 / 373,708 | 0 / 0 | 36 / 37,847 |
| 62 | 0 / 0 | 26 / 17,755,174 | 0 / 0 | 6,887 / 187,073,641 |
| 63 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 64 | 0 / 0 | 0 / 0 | 0 / 0 | 16,498 / 313,857,391 |
| 65 | 0 / 0 | 7,953 / 88,380,172 | 0 / 0 | 0 / 0 |
| 66 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 67 | 6 / 33,448 | 17 / 151,840 | 5 / 30,740 | 35 / 215,180 |

## Expected per-pattern footprint after Part 2

This is a **new successful run projection**, not a proposed migration of the
measured folders. Keep the measured folders untouched. Cells use the same
files / bytes notation. `same` means exactly the corresponding measured F/B;
`0 / 0` is conditional on the success and evidence-retention rules above.
Gzip figures are estimates, not a claimed benchmark. EVENT cells budget current
bytes conservatively; actual gzip sizes remain to be measured. Successful
cleanup with retained diagnostics can exceed these scratch-free targets.

| ID | T | J | D | JP |
|---|---:|---:|---:|---:|
| 01 | same | same | same | 0 / 0 |
| 02 | same | 0 / 0 | same | 0 / 0 |
| 03 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 04 | same | same | same | same |
| 05 | same | same | same | 0 / 0 |
| 06 | same | same | same | 0 / 0 |
| 07 | same | same | same | same |
| 08 | same | 0 / 0 | same | same |
| 09 | same | 0 / 0 | same | same |
| 10 | same | 0 / 0 | same | 0 / 0 |
| 11 | same | 0 / 0 | same | same |
| 12 | same | 0 / 0 | same | same |
| 13 | same | 0 / 0 | same | same |
| 14 | same | 0 / 0 | same | same |
| 15 | same | 0 / 0 | same | same |
| 16 | same | 0 / 0 | same | same |
| 17 | same | 0 / 0 | same | same |
| 18 | same | same | same | same |
| 19 | same | same | same | same |
| 20 | same | same | same | same |
| 21 | same | 0 / 0 | same | 0 / 0 |
| 22 | same | 0 / 0 | same | 0 / 0 |
| 23 | same | 0 / 0 | same | same |
| 24 | same | same | same | same |
| 25 | same | 0 / 0 | same | 0 / 0 |
| 26 | same | 0 / 0 | same | same |
| 27 | same | 0 / 0 | same | 0 / 0 |
| 28 | 1,587 / ~63,357,005 | 96 / ~96,585 | 525 / ~5,688,349 | 0 / 0 |
| 29 | 529 / ~5,839,129 | 0 / 0 | 0 / 0 | 0 / 0 |
| 30 | 1,272 / ~4,609,851 | 0 / 0 | 435 / ~4,572,888 | 0 / 0 |
| 31 | 954 / ~169,210,786 | 0 / 0 | 290 / ~25,856,882 | 0 / 0 |
| 32 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 33 | 1,272 / ~118,144,043 | 0 / 0 | 435 / ~18,321,818 | 0 / 0 |
| 34 | 0 / 0 | 0 / 0 | 3,750 / ~184,457,149 | 0 / 0 |
| 35 | 0 / 0 | 0 / 0 | same | 0 / 0 |
| 36 | same | 0 / 0 | same | 0 / 0 |
| 37 | same | same | same | 0 / 0 |
| 38 | same | 0 / 0 | same | 0 / 0 |
| 39 | same | 0 / 0 | same | 0 / 0 |
| 40 | same | 0 / 0 | same | 0 / 0 |
| 41 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 42 | 0 / 0 | 14 / budget 3,337,723 | 310 / budget 265,003,234 | 6 / budget 158,828 |
| 43 | 0 / 0 | same | 0 / 0 | 0 / 0 |
| 44 | 0 / 0 | same | 0 / 0 | 0 / 0 |
| 45 | 0 / 0 | same | 0 / 0 | 0 / 0 |
| 46 | 0 / 0 | same | 0 / 0 | 0 / 0 |
| 47 | 0 / 0 | same | 0 / 0 | 0 / 0 |
| 48 | 0 / 0 | same | 0 / 0 | 0 / 0 |
| 49 | 0 / 0 | 0 / 0 | 0 / 0 | same |
| 50 | 0 / 0 | 0 / 0 | 0 / 0 | same |
| 51 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 52 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 53 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 54 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 55 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 56 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 57 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 58 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 59 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 60 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 61 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 62 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 63 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 64 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 65 | 0 / 0 | same | 0 / 0 | 0 / 0 |
| 66 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 67 | same | same | same | same |

The EVENT counts include persistent locks: J has 100 compile events in seven
index contexts; its three developer pools have one event/context apiece. D has
20,277 events across 155 contexts, including index zero (179 commits do not
imply 179 contexts: skipped/no-compile commits contribute no events). D has 465
nonzero-exit finished events and J has six; full diagnostics belong in their
finish records, without creating one extra file per failed event. D has 20,277 starts but only 20,276 finishes and javac logs: the total is
`20,277 + 20,276 + 20,276 = 60,829`. One start has no finish even though the
experiment reports completion. Preserve this incomplete event. No toolchain files
are present in these measured event trees, but the current writer can produce
them and the new schema must retain them when present.


For orientation, summing the per-pattern targets (including unchanged history,
all protected generated data, retained miscellaneous files, and the conservative
EVENT byte budget) gives:

| Snapshot | Current files / bytes | Projected files / bytes | Interpretation |
|---|---:|---:|---|
| T | 17,828 / 8,598,089,917 | 17,510 / ~462,747,503 | Mostly compression; no work remains to clean |
| J, including pools | 1,045,976 / 18,230,835,259 | 52,436 / ~1,293,829,623 | Mostly successful scratch cleanup |
| D | 74,477 / 4,247,106,979 | 13,813 / ~571,296,103 | Compile-event consolidation and compression |

These are planning targets, not before/after execution results. They assume
eligible stages succeed and the evidence-retention decisions below are resolved.
They exclude any newly introduced durable diagnostic packaging or replacement
KTester completion file; those bytes must be measured and reported separately.
They also conservatively retain miscellaneous files, even under scratch roots.
No size savings are claimed for histories or policy batches/runs: these are
outside Part 2.4's requested compression list. `--keep-work` disables C1, C2,
C2END and KT cleanup but does not disable gzip or event consolidation.

## Checkout and scratch lifetime design

The source references below describe the current code, not completed changes:

| Consumer boundary | Current behavior / dependency | Proposed safe boundary |
|---|---|---|
| [prepare.run](../src/test_survival/stages/prepare.py) | Builds all indices up front; skipped commits get only records | Keep anchor; after prepare succeeds, release successful nonzero indices and markers. Optional per-build release bounds prepare peak to anchor plus build workers but requires the failure-retention agreement below. Preserve failed build working evidence. |
| [JavaMavenAdapter](../src/test_survival/adapters/java_maven.py), `folder`, `checkout`, `build`, `worker`, `setup_runner` | `folder` only constructs a path; build reuse requires `target/classes`. Worker launch uses index 0 as cwd; setup reads its POM | Add one synchronized ensure-built path before actual worker dispatch. Ensure anchor before setup/inventory/renderer even if prepare was skipped. A cached result read need not recreate a checkout. Never rebuild the same index concurrently. |
| [survive.run](../src/test_survival/stages/survive.py), [dispatch](../src/test_survival/runner/dispatch.py) | Parallel `(index, group)` jobs share one built production tree; derived groups read raw only | Acquire the index for all active consumers; release only after jobs and coordinator validation succeed. If any group fails/interruption occurs, retain the index and scratch. Derived aggregation needs no source tree. Recreate in cover when needed. |
| [cover.run](../src/test_survival/stages/cover.py) | Queues all index/group jobs, never calls build; reads predecessor universes, including skip aliases | Ensure a bounded set of indices, not every queued future. All groups at one index must finish before release. Persist XML/CSV/raw first; validate their completeness. Pre/post-image filtering and aggregate use lines CSV, so do not retain source trees for them. |
| [scope.run](../src/test_survival/stages/scope.py) | Anchor accessor audit and per-method coverage; consumes CSV after workers return | Keep anchor. Clean per-method scratch after the scope stage's denominator and audit outputs succeed; retain failure scratch. |
| [screen_round](../src/test_survival/stages/screen.py) | Reads `record.compile.log` or `record.parse.log` from work after worker return; generate invokes screen, then screen invokes it again and can reuse validation JSON | Do not delete validation diagnostics after generate. Keep until later screen succeeds, or make durable diagnostic copies with compatible lookup. A future screen rerun must still classify cached failed candidates without missing-log errors. Never rewrite generated validation records just to relocate a log. |
| [policy Runs](../src/test_survival/policy/execute.py), `initialize`, `run_index`, `prefetch` | Already builds before measuring; one index can have both window and regeneration batches; many digest outputs share a batch | Release after both consumers and all corresponding digest records are durably complete. Batch completion alone is insufficient. Keep anchor through policy completion; policy cover/report read saved data and need no Java. |
| [build_pool](../src/test_survival/policy/pools.py), [snapshot](../src/test_survival/policy/artifacts.py) | Build index 0, inventory, select/generate/screen, snapshot, write pool.json; coordinator records step completion later | Validate pool member sources, helpers, resources and execution hashes; commit the pool's completed step; then remove owned scratch, with exceptions. On recovery, a completed pool with intact verified results may finish an interrupted cleanup without regenerating the pool. |
| [KTesterTool](../src/test_survival/generators/ktester_tool.py), [completion.record](../src/test_survival/generators/completion.py) | Dataset/index reused by rounds; report writes `work/ktester/<g>/experiment-result.json` only when dataset_info exists | Keep dataset through report; preserve completion metadata in place or move to a documented durable result with a legacy reader. Before removing round scratch, verify all model responses/attempts were archived into generated. |
| [report renderer](../src/test_survival/report/figures.py) | PNG render uses `folder(0)` as working directory | Anchor deletion belongs after report and KTester completion, not after cover or before report launch. |
| [pipeline.run_session](../src/test_survival/pipeline.py), [policy pipeline](../src/test_survival/policy/pipeline.py) | A successful `--until prepare` or `--until pools` invocation also writes run status `completed` | Do not mistake invocation completion for whole-experiment completion. Final anchor cleanup requires all required stages current/completed; partial runs retain anchor and necessary cross-stage scratch. |

For a running index, cleanup must have exclusive ownership under the existing run
lock plus a per-index synchronization/refcount strategy shared by build and worker
consumers. A worker returning exit zero is not sufficient: the coordinator can
still reject missing methods, kill signals, unavailable coverage, or failed
control verification. Keep work on these paths. Do not clean in a `finally`
block that also executes on `BaseException`/interrupt. Do not remove compiler
cache locks, process locks, the configured read-only baseline, or external reuse
sources. Follow archive's no-follow/containment approach for deletion, including
symlink-swap regression cases. Pools' `state/run-owner.json` ownership must remain
valid until all pool workers drain.

Cleanup itself needs idempotence across interruption: publish result and completed
state before deletion, recognize partially removed scratch on resume, and never
let scratch absence invalidate a completed scientific result. Failed cleanup
should retain a diagnostic and be retryable; it must not trigger fresh model
calls. Retain already-written evidence if an interruption happens during result
publication. Final checks must verify all expected logical results, not merely
that `pool.json` exists.

## Constraint conflicts and alternatives to agree before Part 2

### Early release versus retaining an entire failed stage

Deleting each successful prepare index immediately would bound peak source copies,
but a later prepare failure would find those earlier working trees already gone.
That does not meet a literal requirement to leave *all* working files of a failed
stage. The conservative implementation must wait for whole-stage success before
cleaning prepare work (and similarly retain scratch until survive/cover succeeds).
It still removes accumulated trees between stages and after completion, but cannot
claim a bounded peak inside the current eager prepare stage. Proposed alternative,
if agreed: treat each verified index build as a durable sub-operation whose work
can be released, retaining every failed/interrupted/in-flight index and all
original results. Otherwise defer the peak improvement; do not silently redefine
stage success. The end-to-end report must state which boundary was implemented.

### Rebuilding can overwrite measurements and change build decisions

`build()` overwrites `results/builds/<i>.json` and build log paths when its classes
are absent. Re-running Maven can change the offline/online attempt list, cache
hits, dependency contents, or even the build verdict. This violates the requested
comparison beyond timestamps and can change which indices are evaluated.
Recreation must use the existing checkout/build machinery but distinguish a
**workspace restoration** from a new measurement: retain the authoritative build
record and its original logs, record restoration diagnostics separately, require
the same recorded SHA and dependency identities, and stop with work retained if
the reconstruction disagrees. Do not silently change build-fail to build-ok or
vice versa. Pin dependencies/container identity for verification; git alone is
not sufficient to reproduce an arbitrary Maven build.

### Pool work removal versus compiler cache and durable evidence

“Remove a pool's work” conflicts with “do not remove work/compile-cache” if a pool
has a cache. Preserve the pool-local `work/compile-cache` and its lock inodes;
remove only owned eligible siblings. Moving caches to a shared parent is a later
option, not an implicit part of this change. Likewise, `work/generation-history`
and `work/policy-history` can contain prior `generated/` trees; leave histories
intact. KTester completion must be retained as described above. Generated sources,
responses, prompts, repair originals, funnels and validation records are not
cleanup targets, including those copied into policy snapshots.

### Failure evidence can outlive a successful job or stage

Expected test/compile failures are measured outcomes and can coexist with a
successful survive/screen stage. A failed *stage* or interrupted stage must retain
all its owned work. At a successful-stage cleanup boundary, retain full failed
compiler diagnostics durably in the event stream; retain any other diagnostic
that has no durable equivalent (parse logs, failed test launcher/XML reports,
coverage errors, KTester partial responses). Initially it is acceptable to keep
those small failure subtrees and delete only successful classes/trees. This
makes actual savings lower than the scratch-free projection; report that
separately rather than dropping evidence. Screening logs in particular remain
necessary for cached classification on future screen runs.

### One completed compile-event line cannot record a killed compiler

The current [CompilationService](../src/test_survival/adapters/compilation.py)
writes `started.json` before compilation and `finished.json` afterward, with
optional `interrupted.json` and `toolchain.{json,log}`. Waiting to append a single
combined line until finish loses a SIGKILL's start evidence. Proposed alternative:
append a `start` record and a `finish` or `interrupted` record linked by an event
ID; each retains the old payload. Use one independently closed gzip member per
append, `flock` across processes, flush/fsync before releasing the lock, and a
persistent per-context lock file. Include SHA/group/phase/work identity inside
the record; index zero multiplexes screening, control and method coverage.

Proposed successful javac output limit: **16 KiB of UTF-8 bytes**, retaining the
original byte count, SHA-256 and an explicit truncation flag. Store the complete
text inline for a nonzero exit, timeout, or exception; preserve toolchain probe
payloads and logs as well. Test Unicode truncation. Normal readers read concatenated
gzip members; an interrupted last append must not hide earlier complete events.
Recovery under the lock should preserve the damaged tail separately as failure
evidence before restoring a valid append boundary. Locks alone do not make gzip
crash-safe. Readers must still understand old event directories, and raw
`cache.event`/`origin_event` references must resolve both layouts. If preserving
one line per compile is mandatory, a durable separate start journal is required;
it cannot be obtained from finish-only logging.

### Transparent reads need more than an open function

Route `io.read_json` and `read_csv` through one shared logical-file opener, plus
matching logical existence/enumeration helpers. Audit:

- [java_worker](../src/test_survival/adapters/java_worker.py) has its own JSON
  writer, direct build JSON reads and `ET.parse`; [java_maven](../src/test_survival/adapters/java_maven.py)
  checks per-method XML existence. JaCoCo must write a temporary plain XML before
  conversion/compression; publish gzip atomically only after successful parsing.
- [survive](../src/test_survival/stages/survive.py), [cover](../src/test_survival/stages/cover.py),
  [consistency](../src/test_survival/report/consistency.py), [tables](../src/test_survival/report/tables.py)
  count `.exists()` and `glob('*.json')`, not just read contents. Count each logical
  record once, even if both representations exist. Quarantine/rename must use the
  resolved physical file. Cover prerequisite checks and line-universe lookup
  must accept gzip before deciding that coverage is missing.
- [policy Baseline](../src/test_survival/policy/baseline.py) chooses raw-derived
  versus raw with existence checks; [policy cover](../src/test_survival/policy/cover.py)
  chooses a group's coverage universe similarly. Preserve imported raw-folder
  provenance rather than inferring it from today's configuration.
- [dev_results](../src/test_survival/dev_results.py) and [importer](../src/test_survival/importer.py)
  enumerate/copy files and hash physical bytes. Resolve logical suffixes and hash
  decompressed content; keep old manifest entries valid. Do not blanket-decompress
  source trees, model files or unrelated binary assets.
- Progress, status/show, [dashboard](../src/test_survival/dashboard/build.py),
  report, archive dry-run/archive restore, and `rehash` need old-only, new-only and
  mixed-layout fixtures. Summary CSV/JSON, state, report tables, weekly reports and
  direct export inputs stay plain. Archive file hashes still describe the actual
  stored ZIP/file bytes; scientific content hashes describe uncompressed bytes.

If both `name` and `name.gz` exist, validate equal logical content or reject the
ambiguity; silently taking a stale plain file is unsafe. Do not change completed
stage hashes for a storage-only option; keep `--keep-work` out of result/stage
fingerprints and forward it to background children and nested pool adapters.

### dev-scope XML and strict output equality

No current downstream calculation reads a group coverage XML after the worker
creates its lines CSV. Report and policy coverage read CSV. Imports copy the XML
as evidence but do not calculate from it. `run_group` reuse does not require XML;
`method_coverage` does require its own XML, which is a different path. Thus the
requested dev-scope omission saves 318 files / 902,248,567 raw bytes in T and
145 / 206,938,674 in D. It must retain JaCoCo execution and lines generation:
write transient XML, produce/verify CSV, then omit the durable successful XML.
Do not substitute Dev coverage for Dev-scope coverage; they execute different
method subsets despite similar file sizes.

However, XML is listed in `result-formats.md`: deleting that output cannot satisfy
literal “compare every listed output byte-identically.” Proposed agreement:
compare the transient dev-scope XML with the reference before cleanup, record its
uncompressed content hash in verification evidence outside the experiment, and
check identical published CSV; allow only this explicitly requested logical-file
omission. Otherwise retain it as `.xml.gz` for the first implementation.

The same comparison problem applies to compile-event layout, additional workspace
restoration evidence, and KTester completion relocation. Test their semantics and
old-reader compatibility separately; do not pretend a different file layout is
byte-identical. No exclusion of scientific measurements is proposed.

## Proposed end-to-end verification configuration

No experiment has been run for Part 1. The following is a concrete **proposed
fixture to agree before Part 2**, not a claim of measured runtime or a proof that
all possible inputs are equivalent. Real Java/Maven/JUnit/JaCoCo execution is
required; mocked adapter tests alone are insufficient.

Use a new, disposable Git repository at `/tmp/footprint-e2e/target`, with a
single-module Maven/JUnit 4 project, pinned compiler/Surefire/dependency versions,
and a recorded immutable `survival-java:25` image ID built from this repository's
`docker/java`. Python is 3.12. Warm the Maven dependency cache once before timing,
then give each comparison run an identical copy of that warm cache; do not share
experiment compile caches. Record tool commit, image ID, JDK, Maven, dependency
checksums and target commit SHAs in external verification metadata.

Fixture source and history specification:

| Commit/tag | Concrete contents / trigger |
|---|---|
| `fixture-t` (anchor) | `fixture.Calculator` has three small branch-bearing methods: `value()` returns 1, `nonNegative(x)` clamps negatives to zero, and `label(x)` returns a sign label. Four passing developer methods across two classes exercise these branches, including one resource/reflection-only helper used from `src/test/resources` and `src/test/java`. Two imported generated-style tests cover `value()` and `nonNegative()`. |
| index 1 | README-only change; skipped with `skip_untouched: main-sources`. Exercises predecessor aliasing from index 2 back to 0. |
| index 2, tag `fixture-release` | Change `value()` from 1 to 2 and update the developer test in Git to expect 2. The frozen anchor test and generated-style value test fail; the newly selected developer pool's updated test passes. |
| index 3, tag `fixture-end` | Change the `label` branch implementation while preserving its tested behavior; forces another production coverage measurement and executes the newly regenerated pool after its publication. |

Keep the committed fixture identical for all runs (including Git dates). Create a
small *synthetic Ouédraogo-shaped import fixture* outside all three measured
experiments: `generated/ouedraogo/round1/sources/Calculator/ZSL/*.java`, an honest
fixture `funnel.json`, and a generation/unit-mapping record for Calculator. It
contains authored deterministic tests, not purported paid model output. Import
with the existing `survival import --what generated` path after scope. This covers
an Ouédraogo group through real screen/control/survival/coverage without two
nondeterministic model calls. It does not test live model generation or KTester
index construction; cover those cleanup/resume boundaries with saved fixtures and
focused tests. If actual model-authored provenance is required, prepare one new
small Ouédraogo round once after confirmation and reuse those immutable bytes
for all comparison runs. Do not copy from the measurement-only experiments.

Baseline `experiment.yaml` (same file for both tools; defaults omitted are the
current validated defaults):

```yaml
name: footprint-fixture
project:
  repo: /tmp/footprint-e2e/target
  language: java
  build: maven
  test_framework: junit4
  module: .
anchor: fixture-t
range:
  branch: main
  end: fixture-end
  limit: 3
  skip_untouched: main-sources
scope:
  rule: explicit
  classes: [fixture.Calculator]
  min_methods: 1
  target_methods: [1, 10]
  min_classes: 1
  max_classes: 1
  exclude_accessor_heavy: false
dev:
  stable_runs: 3
  unmapped: error
  groups:
    - {name: dev, denominator: project}
    - {name: dev-scope, denominator: coverage-scoped, derive_from: dev}
generators:
  - name: ouedraogo
    method: ouedraogo
    impl: native
    params: {prompts: [ZSL]}
    rounds: {max: 1, until_dev_matched: false}
screen: {stable_runs: 3, dedup: exact-body, matching: none}
coverage: {changed_line: true, at_anchor: true, groups: alive-entering}
runner:
  javac_mode: subprocess
  docker:
    image: survival-java:25
    m2_volume: footprint-e2e-m2
  workers: {build: 1, survive: 1, cover: 1}
  timeouts: {build: 120, compile: 60, test_class: 30}
  jvm: {launcher: 512m, javac: 256m, jacoco: 512m}
```

Put the baseline at `/tmp/footprint-e2e/target/experiments/baseline` and a separate
policy experiment at `.../experiments/policy`. For the policy configuration, copy
the baseline YAML and append:

```yaml
policies:
  baseline: experiments/baseline
  regenerate_developer: true
  groups: [dev-scope]
  releases: [fixture-release]
  concurrency: {dev_selection: 1}
  fill_seed: 20261029
  set:
    - {name: release-reset, trigger: release, action: reset}
    - {name: release-fill, trigger: release, action: fill}
```

Both policies share exactly one developer pool at index 2. This exercises pool
anchor cleanup, full-suite per-test selection, strict three-run screening,
snapshot hashes/resources, window and regeneration batches, and a later index.
Using only dev-scope for policy regeneration avoids new model work. Baseline still
measures all three groups, including raw-derived output and per-method coverage.

Run sequence, after configuration/fixture preparation:

```sh
survival run /tmp/footprint-e2e/target/experiments/baseline --until scope --no-panel
survival import /tmp/footprint-e2e/target/experiments/baseline --from /tmp/footprint-e2e/generated-fixture --what generated
survival run /tmp/footprint-e2e/target/experiments/baseline --until report --no-panel
survival policy run /tmp/footprint-e2e/target/experiments/policy --until report
```

Run the reference on pinned main **before implementation**, preserve its entire
experiment folders outside the canonical run location, then restore a clean
fixture location and run the branch at the same paths. This avoids path-derived
input/cache hashes varying just because the experiment directories differ.
Never overwrite reference artifacts. Preserve identical generated import bytes.
After an uninterrupted branch run, do a third clean branch run, stop it once
while a real policy-survive batch is active (worker registration and start/event
observed), assert retained work and failed/interrupted state, then resume without
`--force`. Compare this run with both reference and uninterrupted branch. This
third run is needed to separate interruption effects from the layout change.

Expected budget with warmed dependencies: approximately 10–25 minutes per full
baseline-plus-policy run, target **under one hour each**. The workload is four
build points including anchor, four developer methods, two generated-style
methods, three coverage indices and one pool. This estimate is unbenchmarked;
Maven downloads/backoffs and Docker startup must be timed separately. If the main
trial exceeds an hour, stop and reduce the fixture workload before agreeing it
as the verification gate; do not claim the time requirement was established.

### Comparison contract and explicit exclusions

Enumerate **every output pattern in `docs/result-formats.md`**, both root and
pool instances, plus policy output patterns in `docs/regeneration-policies.md`.
Create a logical-path manifest that strips only the storage `.gz` suffix, verifies
no ambiguous twins, decompresses, and records content SHA-256. Compare all paths,
not only paths present on both sides; an unexpected missing file fails the check.
Maintain a checklist of patterns absent because this fixture does not exercise
them (e.g. KTester completion, live model responses, quarantine, legacy dashboards,
archive manifests); exercise those with dedicated fixtures rather than calling
them end-to-end covered. Retained model/import fixture content must be identical.

Byte comparison rules:

- CSV/Markdown/SVG and ordinary deterministic JSON: compare exact bytes after
  narrowly specified volatile-value substitution; do not reorder method arrays,
  sort CSV rows, round percentages, or remove unexpected fields. PNG: compare
  exact bytes with the same JDK renderer, investigate any difference.
- Timestamp exclusions: stage/run `started`, `finished`, import `imported`, anchor
  `started`, model `timestamp`/prompt `checked_at`, compile-event start/finish,
  archive `archived_utc`, and JaCoCo session `start`/`dump`. Commit dates remain
  exact because they are input data, not execution timestamps.
- Duration exclusions: command/result `seconds`, compile `cache.elapsed_seconds`,
  summary and KTester `phase_seconds`, model `latency`/call `seconds`, and the
  `seconds` column in `dev-method-coverage-t.csv`. JUnit test/report `time` fields
  are durations when comparing retained baseline reports.
- Absolute-path normalization: only known fixture/tool roots in command arguments,
  `cwd`, source/import/baseline/report paths, and recorded log/error paths. Preserve
  relative artifact identities. A path-looking string in a test failure message
  is not automatically excluded; document each substitution.

**The user's timestamp/duration/absolute-path exceptions alone are insufficient
for every documented artifact, even on two unchanged-tool runs.** Other observed
schema differences that need an explicit decision before a final equality claim:

| Difference | Why the narrow exception does not cover it | Proposed verification, without changing saved measurements |
|---|---|---|
| Raw `runner_sha256`; compile key/implementation digest | Tool sources necessarily change during this feature; absolute paths are not the cause | Verify each map against its actual tool revision, allow only hashes of reviewed changed components to differ, compare all measurement fields. Never replace truthful provenance with main's hash. |
| UUID event/origin references, lifecycle PID/run/container/worker IDs | Random/process identities differ on identical reruns | Validate referential integrity and event lifecycle, canonicalize IDs consistently only in comparison copies. Do not change stored IDs. |
| JaCoCo XML `sessioninfo.id` | Random JVM session identifier; a string, not a timestamp | Canonicalize session identities in comparison copies, retain counters, class IDs, lines and all other XML exactly. |
| Cached compile hit/miss, cache keys, build attempts; killed-and-retried event counts | Cleanup/rebuild and intentional interruption change operational history | Keep original build measurement records; verify extra restoration/retry history separately. Do not exempt survival method state, requested methods, coverage, or denominators. |
| `final_summary_sha256`, import `source_sha256` / file checksums when their contents contain allowed volatile fields | A digest of volatile metadata differs even after normalizing the displayed payload | Verify the original digest against its original uncompressed bytes; separately compare normalized payloads and any deterministic source hashes. |
| Dropped dev-scope XML / changed compile-event layout | Explicit requested output-shape changes | Compare transient XML and event semantics as described above, with an explicit path allowance; otherwise retain compressed XML. |

These are **proposed additional comparison rules, not silently accepted
exclusions**. If the literal exception list must remain unchanged, report the
full-output comparison as failed/blocked even when measured tables agree. Do not
weaken stale-stage detection or lie about provenance to manufacture equality.
The non-negotiable comparison is exact survival states, first failures, requested
and frozen methods, coverage line universes/hits, changed-line/exposure totals,
policy pools/selections/segments/curves, and every report's measured numbers.

Use single survival worker in the equivalence fixture to avoid scheduler-dependent
extra executions/dead markers described in `result-formats.md`. Separately exercise
parallel workers with deterministic barriers, asserting identical aggregates and
safe lifetime management. Parallel raw evidence is not guaranteed byte-identical
by the current main implementation because completion order affects dispatch.

### Unit/fixture gates for the four implementation commits

1. **Checkout cleanup:** old completed prepare with no checkout; restored index
   zero before setup/renderer; survival then coverage recreation; two groups
   sharing an index; policy window plus regenerate batch; skipped/build-failed
   indices; same-index build locking; recorded build preservation and restoration
   disagreement; failed/interrupted owner retention; completed pool verification;
   partial cleanup resume; symlink escape protection. Do not introduce unsafe
   hard links. Existing parallel-survival and policy-regeneration tests are
   relevant regression suites.
2. **Stage work:** success versus exception/KeyboardInterrupt/SIGKILL; cleanup
   only after coordinator validation; validation-log reuse from generate into
   screen; incomplete KTester responses preserved; histories/generated/cache/lock
   protection; `--keep-work` foreground/background/pool propagation; partial
   `--until` runs and idempotent cleanup. Verify generated tree hashes and mtimes
   across cleanup operations.
3. **Compile events:** concurrent process appends in one index; multiple indices;
   start without finish; tail truncated inside a gzip member; failure and timeout
   full logs; success truncation boundary and Unicode; toolchain evidence; old
   event-directory readers and event reference resolution; stable lock inode.
4. **Compression:** old-only/new-only/mixed files, conflicting twins, interrupted
   atomic publication, malformed gzip, plain/gzip equal hashes, logical glob counts,
   imports and re-exports, quarantine rename, per-method XML reuse, skipped raw,
   derived provenance, old completed pools, `rehash` status equality. Exercise
   status, every show stage, reports, dashboard, policy planning/cover/report,
   archive round-trip and restored status using old-layout fixture copies.

After each meaningful change run its focused tests, then the repository's Python
suite and required real-Java fixture checks before PR delivery. Do not use the
measurement-only experiments as mutable test fixtures. Generate no new tests for
this documentation-only Part 1 commit; validate its tables, references and YAML.

Finally record file counts, logical bytes, allocated bytes, and directories for
main, uninterrupted branch, and interrupted/resumed branch, separately for
baseline/policy/pools and combined. Keep generated bytes, compiler cache bytes,
retained failure evidence, and successful scratch savings separate. Sample peak
live checkout count/bytes during prepare, survive, cover and pools as well as
final totals; a zero final footprint alone does not prove the source-copy peak
was reduced. Assert all intended successful checkouts are absent, failed work
remains, completed pools were not regenerated, and status/report/dashboard still
load without work.

## Part 1 delivery boundary

Commit only this review on `feat/footprint`, push it, and stop. Part 2 requires
confirmation of the configuration and the conflicts above. The implementation
phase will use four separately tested commits, update `README.md` and
`docs/result-formats.md` for actual changed paths/options, push the branch and
open a PR without merging. No implementation PR or experiment execution is
claimed by this document.
