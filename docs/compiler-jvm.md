# Opt-in compiler JVM reuse

The default remains one `javac` process per compile. On a JDK 25 runner, enable
reuse explicitly in a new experiment configuration:

```yaml
runner:
  javac_mode: reused-jvm
```

Use `subprocess` to select the default. No CLI command changes are required.
Changing this setting changes runner fingerprints. Implementation provenance also
changes when upgrading the tool, so existing results can require recomputation;
do not switch a historical experiment in place just to compare performance.

## What is reused

Each `run_group` owns a compiler session. Requests run sequentially, with a fresh
`JavaCompiler`, `StandardJavaFileManager` and `CompilationTask` every time. File
managers close after each request. The JVM is replaced after 64 requests, and on
cwd/JDK-file/heap/environment changes. JDK executable, release, module archive,
symbol archive and JVM library metadata are checked before each request. All
processes close at group exit.
For example, a failed suite followed by 213 file compiles can use four compiler
JVMs, plus the existing toolchain identity probe. Concurrent groups have separate
sessions; there is no experiment-wide shared JVM. JUnit continues in separate JVMs.

This changes compilation execution, not which tests run. The frozen sources,
method IDs, denominator, whole-suite-to-file fallback, environment retries and
coverage selection remain the same. A normal failed compilation preserves its
diagnostic text and partial class output for the existing fallback policy.

## Supported inputs and fallback

The first server handshake must identify JDK **25**. An unqualified JDK falls
back to the CLI, and its failed qualification is remembered for the current
environment so every file does not start another unqualified server.

The accepted command contains only `javac`, its heap option, explicit absolute
classpath/sourcepath/output paths and explicit Java source files. Unknown options,
`@argfiles`, wildcard/relative classpaths, missing inputs, manifest `Class-Path`,
compiler plugin registrations and unsafe output locations use the original CLI.
External `JDK_JAVAC_OPTIONS`, `JDK_JAVA_OPTIONS`, `JAVA_TOOL_OPTIONS`, `_JAVA_OPTIONS`
and native-library injection settings also select the CLI. The helper's startup
classpath is fixed to its own directory; ambient `CLASSPATH` cannot load helper
plugins. Requested compiler classpaths are still passed unchanged.

An annotation processor **registration alone** is allowed: on JDK 25, processing
requires explicit configuration. The restricted command shape has no such
options, and the external option variables above are excluded. Both the CLI and
API were tested with a sentinel processor that records and fails on activation:
registration-only stayed inactive; explicit `-proc:full` used the CLI and activated
it. No `-proc:none` flag is added, and active processing is never emulated.
Plugin registrations remain excluded because plugins may auto-start without an
explicit option. An auto-start sentinel plugin exercises that fallback too.
See the [JDK 25 javac specification](https://docs.oracle.com/en/java/javase/25/docs/specs/man/javac.html#annotation-processing).

Timeout kills/reaps the compiler process group, clears partial output and returns
the timeout without another compile. A crash, task exception or protocol error
discards the JVM, preserves its diagnostic log, clears partial output, then tries
the CLI within the remaining timeout. Later requests start a clean JVM. These
fault paths may cost an extra attempt; they do not create negative cache entries.

## Cache and observations

The existing success-only CompilationService remains responsible for cache keys,
validation, publication and event records. Backend implementation hashes separate
CLI/API cache entries. Its conservative processor/plugin cache bypass is unchanged:
registered-but-inactive processors can reuse the JVM while still compiling every
request. No compiler failure or timeout is cached. Cache hits execute no compiler.

Actual compilation records retain `command`, `cwd`, `exit_code`, `timeout`,
`seconds` and `log`. They add `compiler_backend`:

- `mode: reused-jvm`, `pid`, `cold` and `request` identify reused execution.
- `mode: subprocess` with `reason` identifies a compatibility fallback.
- `discarded` identifies a discarded worker after a fault.

PID is scoped to its container; it is not globally unique across jobs. The request
counter is session-wide and continues across restarts. Successful API durations
include compatibility checks and cold startup; compiler-cache hashing/validation
has its existing separate event overhead. The recorded command remains the
equivalent javac invocation; `compiler_backend` distinguishes its actual executor.
First-start stderr is retained next to the first log as `.jvm-stderr`; failure
diagnostics are copied to `.jvm-failure` before CLI fallback overwrites the log.

## Verification and limits

The Java CI job runs default and reused worker fixtures, including retries,
suite failure with mixed file results, unchanged denominator, cache miss/hit and
coverage equality. Protocol fault tests exercise truncated/bad responses, crashes
and timeout cleanup. Real-JDK tests compare status, complete diagnostics and class
hashes over success/failure/success, reversed order, changed cwd and production
class replacement at the same path, processor activation and plugin fallback.

Three independent sessions each execute 66 requests, crossing the real recycle
boundary. Every result is compared with a fresh CLI baseline. The fixture verifies
distinct JVMs, no observed descriptor growth (tolerance three), per-process RSS
below 768 MiB, and final process reaping, under a 180-second container cap. This is
a bounded regression check, not proof of arbitrary long-run memory stability.
See [CI instructions](../ci/README.md) to reproduce the checks.

A live compiler JVM can overlap the separate JUnit JVM: account for both when
choosing worker counts and heap sizes. Fresh managers and periodic restart limit
state lifetime but are not a hard total-memory bound. Cold startup and input checks
can make short/fallback-heavy groups slower. Small copied-ta4j comparisons validate
representative results; they do not establish full-experiment speedup. Keep the
default until a normal experiment measures result equivalence, stage wall time,
actual API/CLI/cache counts, cold starts, fallbacks, faults and peak memory.

### Bounded copied-ta4j check

On an arm64 Mac with Temurin 25.0.4.1+1, a two-CPU/2-GiB container compared four
copied cases with one warmup and three measured repetitions each. Original inputs
and Maven dependencies were read-only, networking was disabled, and result caches
were bypassed in both modes. Backend and case order alternated. The final isolated
run finished in 12.63 seconds under a 60-second cap; an earlier overlapping run
was used only for equivalence, not timing. Together they stayed below the agreed
two-minute measurement budget.

| Case | CLI median | Reused JVM median |
|---|---:|---:|
| index 7 BarSeriesTest success | 572 ms | 112 ms |
| index 7 BooleanTransformIndicatorTest failure | 434 ms | 84 ms |
| index 21 BarSeriesTest helper failure | 537 ms | 141 ms |
| index 106 BarSeriesTest with inactive processor registration | 532 ms | 174 ms |

All 16 pairs agreed on status, complete diagnostics and every emitted class hash,
including partial failure outputs. The success case's 25 JUnit method identities
and observations also agreed. These were 32 actual compilation requests: 16 CLI
JVMs versus 16 API requests in three server JVMs. Cold server source-launch compiles
the helper in-process; that work is included in the 549–565 ms first-request times.
Cold startup was slower than two matching CLI warmups. Compiler invocations were
not skipped, and this is not a full-experiment speedup measurement.
