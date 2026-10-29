"""Ouédraogo context regressions; never invoke an experiment or model."""
from pathlib import Path
from types import SimpleNamespace
import os
import subprocess

import pytest

from test_survival.generators.parts import context
from test_survival.generators.parts.prompts import gate
from test_survival.io import read_json

HELPER = Path(context.__file__).with_name('java')/'DependencyInventory2.java'


@pytest.mark.parametrize('header', [
    'class PackagePrivate {', 'interface ExecutionCustomizer {', 'enum Choice {',
    'abstract class Base<T extends Comparable<T>> {',
    'public static class Nested {',
    'sealed interface Shape permits Circle, Square {',
    'non-sealed class Square implements Shape {',
    'public record Pair<T>(T left, T right) implements Value<T> {',
    '@interface Label {',
])
def test_gate_accepts_java_type_headers(header):
    gate('// public API of p.Type\n'+header+'\n}\n')


@pytest.mark.parametrize('header', [
    'class A<> {', 'interface A< > {', 'record A<>(int n) {',
    'nonsense class A {', 'public A {', 'public class {',
    'class 123 {', 'public interface A', 'class A unexpected {',
    'record A {', 'record A(int n {', 'class A<T {',
])
def test_gate_rejects_malformed_headers(header):
    with pytest.raises(ValueError):
        gate(header+'\n}\n')


def inventory_from_text(tmp_path, text):
    def execute(command, cwd, log, timeout):
        assert Path(command[1]) == HELPER
        assert command[1] != str(adapter.helpers/'DependencyInventory2.java')
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text)
        return {'exit_code': 0}
    adapter = SimpleNamespace(helpers=tmp_path/'legacy-helpers', folder=lambda _: tmp_path,
                              runner=SimpleNamespace(execute=execute),
                              config={'runner': {'timeouts': {'compile': 10}}})
    return context.inventory(adapter, tmp_path)


def test_package_private_inventory_and_skipped_type(tmp_path):
    text = ('T\tp.Owner\tp\tCLASS\tpublic\t\t\t\n'
            'R\tp.Owner\tmethod\tExecutionCustomizer\n'
            'T\tp.ExecutionCustomizer\tp\tINTERFACE\t\t\t\t\n'
            'A\tp.ExecutionCustomizer\t void customize();\n'
            'S\tp.Broken\tBroken.java\tUnavailable source range\n')
    data = inventory_from_text(tmp_path, text)
    block, meta = context.dependency_block(data, 'p.Owner', '', {'max_types': 15, 'max_lines': 400})
    assert 'interface ExecutionCustomizer {' in block
    assert meta['included'] == ['p.ExecutionCustomizer']
    assert 'p.Broken' not in read_json(tmp_path/'prompts/type-inventory.json')
    assert read_json(tmp_path/'prompts/skipped-types.json') == [
        dict(class_name='p.Broken', file='Broken.java', reason='Unavailable source range')]
    # A skipped class may itself be in the generation scope.
    assert context.dependency_block(data, 'p.Broken', '', {'max_types': 15, 'max_lines': 400})[1]['included'] == []


@pytest.fixture
def inventory_java(tmp_path):
    image = os.environ.get('SURVIVAL_JAVA_TEST_IMAGE')
    if not image:
        if os.environ.get('SURVIVAL_REQUIRE_JAVA_TESTS') == '1':
            pytest.fail('Set SURVIVAL_JAVA_TEST_IMAGE for dependency inventory fixtures')
        pytest.skip('Set SURVIVAL_JAVA_TEST_IMAGE for dependency inventory fixtures')
    def run(sources):
        root = tmp_path/'sources'
        root.mkdir()
        for name, source in sources.items():
            (root/name).write_text(source)
        result = subprocess.run(['docker', 'run', '--rm', '--network', 'none',
                                 '-v', f'{HELPER.parent}:/helper:ro', '-v', f'{root}:/sources:ro',
                                 image, 'java', '/helper/DependencyInventory2.java', '/sources'],
                                capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stderr
        return result.stdout
    return run


@pytest.mark.java_integration
def test_inventory_records_sealed_annotations_and_multiline(tmp_path, inventory_java):
    text = inventory_java({'Types.java': '''package p;
        @interface Label { String[] value(); }
        @Label({"{", "...", "class Fake {"})
        sealed interface Shape permits Point, Open {}
        non-sealed class Open implements Shape {}
        @Label({"}", "{"})
        record Point<T>(
            @Label({"{", "..."}) T value,
            String... names
        ) implements Shape {
            public Point { }
            public void accept(@Label({"..."}) String text, String... values) {}
            public record Nested(int n) { public Nested {} }
        }
        class Owner { Point<String> point; Shape shape; Label label; }
    '''})
    assert not any(line.startswith(('S\t', 'D\t')) for line in text.splitlines()), text
    data = inventory_from_text(tmp_path, text)
    assert data[0]['p.Point']['components'] == 'T value, String... names'
    assert data[0]['p.Shape']['permits'] == 'Point,Open'
    assert any('Point(T value, String... names);' in api for api in data[3]['p.Point'])
    assert any('accept(String text, String... values);' in api for api in data[3]['p.Point'])
    assert 'p.Point.Nested' in data[0]
    block, _ = context.dependency_block(data, 'p.Owner', '', {'max_types': 15, 'max_lines': 400})
    assert 'record Point<T>(T value, String... names) implements Shape {' in block
    assert 'sealed interface Shape permits Point,Open {' in block
    assert '@interface Label {' in block


@pytest.mark.java_integration
def test_inventory_skips_bad_type_without_partial_rows(tmp_path, inventory_java):
    text = inventory_java({'Types.java': '''package p;
        class Before { public void valid() {} }
        class Broken { public void first() {} public void broken( {} }
        class After { public void valid() {} }
    ''', 'DefaultPackage.java': 'class DefaultPackage { public void ok() {} }'})
    data = inventory_from_text(tmp_path, text)
    assert set(data[0]) == {'p.Before', 'p.After', 'DefaultPackage'}
    assert not any(line.startswith(('T\tp.Broken\t', 'A\tp.Broken\t', 'R\tp.Broken\t')) for line in text.splitlines())
    assert read_json(tmp_path/'prompts/skipped-types.json')[0]['class_name'] == 'p.Broken'
    assert any(line.startswith('D\t') for line in text.splitlines())


def test_context_helper_preserves_legacy_runner_provenance(tmp_path):
    import hashlib
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.config import validate

    adapter = JavaMavenAdapter(tmp_path, validate(dict(name='context', project={'repo': '/unused'}, anchor='t')))
    legacy = adapter.helpers/'DependencyInventory2.java'
    # This helper is already part of persisted KTester/Dev runner provenance.
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == '366cec31e3bad91f6479a06b2461565ad61bc9f987f001fca21aa0fb8d79fb7c'
    provenance = adapter.provenance()
    assert provenance[legacy.name] == hashlib.sha256(legacy.read_bytes()).hexdigest()
    assert provenance[legacy.name] != hashlib.sha256(HELPER.read_bytes()).hexdigest()
    assert not HELPER.is_relative_to(adapter.helpers)
