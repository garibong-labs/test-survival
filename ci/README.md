# Test CI

`Python tests` runs on every pull request, pushes to `main`, and manual dispatch.
It installs the project's `dev` extra and checks dependencies, Python syntax, and
fast tests on Python 3.12 (the declared minimum) and 3.14. Minor versions intentionally
track supported patch releases. No additional lint/typecheck framework is introduced.

`Java integration` runs on pull requests or `main` pushes changing production code,
tests, Docker/CI files, or `pyproject.toml`, and on manual dispatch. It uses one
Ubuntu 24.04 runner, Python 3.12, and a small Java 25 fixture image. The two official
base images are pinned by digest; the container copies a pinned Python distribution
instead of installing mutable apt packages. Actions are pinned by commit SHA.
The existing `classpath-artifacts.json` pins JUnit, JaCoCo, and other runner JARs by
version and SHA-256. Downloads go only to Maven Central and mismatches fail the job.

The fixtures compile tiny checked-in Java tests, exercise JUnit 4 inventory/selectors,
run the actual worker's environment retry policy, and produce JaCoCo XML/line coverage.
They also compare compiler backends, processor activation, timeout/crash recovery,
suite-to-file fallback and cache/coverage equivalence. Three independent compiler
sessions execute 66 requests each, cross the 64-request recycling boundary, compare
diagnostics/class bytes, and check RSS/file-descriptor bounds under a 180-second cap.
The policy fixtures check per-test JaCoCo resets, parameterized Vintage tests,
multiple source versions in one JVM, and isolation of container teardown probes
in both Jupiter and Vintage.
They do not run ta4j, Maven project builds, or any model calls.

Missing image/library variables fail when `SURVIVAL_REQUIRE_JAVA_TESTS=1`; an absent
or empty library directory always fails. The final JUnit XML check requires all eight named
fixtures to pass with no skips/errors/failures, so missing prerequisites cannot produce
a green integration job. Fast tests deselect the marked Java tests explicitly.

Both workflows use read-only repository permissions, disable persisted checkout
credentials, cancel superseded runs, and set timeouts (10/15 minutes). They never use
`pull_request_target` or repository secrets. Only pip dependency downloads are cached;
compiled tests, experiment outputs, and fixture artifacts are not shared across runs.
No repository protection rules, Actions permissions, or billing settings are changed.

To reproduce the Java fixture on a Docker-capable machine:

```sh
python -m pip install -e '.[dev]'
docker build --platform linux/amd64 -f ci/java.Dockerfile -t survival-java-ci:fixture ci
python ci/java_fixture.py libraries /tmp/survival-java-libs
SURVIVAL_JAVA_TEST_IMAGE=survival-java-ci:fixture \
SURVIVAL_JAVA_TEST_LIBS=/tmp/survival-java-libs \
SURVIVAL_REQUIRE_JAVA_TESTS=1 \
python -m pytest -q --strict-markers -m java_integration tests/test_junit4.py tests/test_compiler_session.py --junitxml=java-results.xml
python ci/java_fixture.py report java-results.xml
```

Update image/action pins deliberately, verify the official source and JAR checksums,
and rerun both workflows. Manual dispatch requires the workflow to be available on the
default branch; new workflow PRs validate through the pull-request trigger first.
