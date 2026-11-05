#!/usr/bin/env python3
"""Create the disposable footprint verification target; never touch real experiments.

Run with the project's Python 3.12 environment. All generated state lives below
/tmp/footprint-e2e. Git commits describe synthetic fixture inputs, not model output.
"""
import json
import os
from pathlib import Path
import subprocess

import yaml

ROOT = Path('/tmp/footprint-e2e').resolve()
TARGET = ROOT / 'target'


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def create():
    if TARGET.exists():
        raise SystemExit('Refusing to overwrite existing fixture target')
    TARGET.mkdir(parents=True)
    def git(*args, date=None):
        env = dict(os.environ, GIT_AUTHOR_NAME='Footprint Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                   GIT_COMMITTER_NAME='Footprint Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        return subprocess.check_output(['git', '-C', str(TARGET), *args], env=env, text=True).strip()
    git('init', '-b', 'main')
    write(TARGET/'.gitignore', 'target/\nexperiments/\n')
    write(TARGET/'pom.xml', '''<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<groupId>fixture</groupId><artifactId>footprint</artifactId><version>1.0</version>
<properties><maven.compiler.release>17</maven.compiler.release><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version></plugin>
<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId><version>3.5.2</version></plugin>
<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-resources-plugin</artifactId><version>3.3.1</version></plugin>
</plugins></build></project>\n''')
    production = TARGET/'src/main/java/fixture/Calculator.java'
    write(production, '''package fixture;
public class Calculator {
 public static int value() { return 1; }
 public static int nonNegative(int x) { return x < 0 ? 0 : x; }
 public static String label(int x) { return x < 0 ? "negative" : "nonnegative"; }
}
''')
    tests = TARGET/'src/test/java/fixture/CalculatorTest.java'
    write(tests, '''package fixture;
import org.junit.Test;
import static org.junit.Assert.*;
public class CalculatorTest {
 @Test public void value() { assertEquals(1, Calculator.value()); }
 @Test public void clamp() { assertEquals(0, Calculator.nonNegative(-3)); assertEquals(4, Calculator.nonNegative(4)); }
}
''')
    write(TARGET/'src/test/java/fixture/ResourcesTest.java', '''package fixture;
import org.junit.Test;
import static org.junit.Assert.*;
public class ResourcesTest {
 @Test public void label() { assertEquals("negative", Calculator.label(-1)); assertEquals("nonnegative", Calculator.label(2)); }
 @Test public void reflection() throws Exception { assertEquals(0, Class.forName("fixture.Helper").getMethod("answer").invoke(null)); }
}
''')
    write(TARGET/'src/test/java/fixture/Helper.java', '''package fixture;
public class Helper {
 public static int answer() throws Exception {
  String text = java.nio.file.Files.readString(java.nio.file.Path.of("src/test/resources/input.txt")).trim();
  return Calculator.nonNegative(Integer.parseInt(text));
 }
}
''')
    write(TARGET/'src/test/resources/input.txt', '-4\n')
    def commit(title, day):
        git('add', '.')
        git('commit', '-m', title, '-m', 'Co-authored-by: Codex <noreply@openai.com>', date=f'2026-01-{day:02}T00:00:00Z')
    commit('fixture: anchor', 1);git('tag', 'fixture-t')
    write(TARGET/'README.md', '# Synthetic footprint fixture\n')
    commit('fixture: documentation only', 2)
    write(production, production.read_text().replace('return 1;', 'return 2;'))
    write(tests, tests.read_text().replace('assertEquals(1,', 'assertEquals(2,'))
    commit('fixture: change value and update developer test', 3);git('tag', 'fixture-release')
    write(production, production.read_text().replace('x < 0 ? "negative" : "nonnegative"', 'x >= 0 ? "nonnegative" : "negative"'))
    commit('fixture: equivalent branch', 4);git('tag', 'fixture-end')
    config = yaml.safe_load("""
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
""")
    policy = {**config, **yaml.safe_load("""
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
""")}
    for name, data in [('baseline', config), ('policy', policy)]:
        write(TARGET/f'experiments/{name}/experiment.yaml', yaml.safe_dump(data, sort_keys=False))
    seed = ROOT/'generated-fixture/generated/ouedraogo/round1'
    source = 'generated/ouedraogo/round1/sources/Calculator/ZSL/GeneratedTest.java'
    write(ROOT/'generated-fixture'/source, '''package fixture;
import org.junit.Test;
import static org.junit.Assert.*;
public class GeneratedTest {
 @Test public void generatedValue() { assertTrue(Calculator.value() == 1); }
 @Test public void generatedClamp() { assertEquals(6, Calculator.nonNegative(6)); }
}
''')
    write(seed/'funnel.json', json.dumps(dict(group='ouedraogo', round='round1', files=1, compiled=1, methods=2,
         stable_3_of_3=2, duplicates=0, kept_this_round=2, total_kept=2, developer_denominator=4,
         ratio_to_dev=.5, per_prompt={}, fixture=True))+'\n')
    write(seed/'generation.json', json.dumps([dict(class_name='Calculator', technique='ZSL', file=source, fixture=True)])+'\n')
    write(ROOT/'fixture-shas.json', json.dumps({name:git('rev-parse', name) for name in ['fixture-t','fixture-release','fixture-end']},indent=2)+'\n')
    print(ROOT)


if __name__ == '__main__':
    create()
