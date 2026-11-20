"""Package and import instructions for the native JUnit 4 prompts."""
import re

HEADING = '// Use this package and copy the imports your test needs.'
NESTED = ('// Write nested types as Outer.Inner. When several outer classes declare the same '
          'simple name, qualify it with the declaring outer class.')
UNNAMED = '// Use the unnamed package. Omit the package declaration.'
# JUnit 4 is requested by all five templates. These libraries are pinned in classpath-artifacts.json.
LIBRARIES = ('import org.junit.Test;', 'import static org.junit.Assert.*;',
             'import org.hamcrest.Matchers;', 'import static org.hamcrest.MatcherAssert.assertThat;',
             'import org.mockito.Mockito;')
NAME = r'(?:[^\W\d]|\$)[\w$]*'
QUALIFIED = rf'{NAME}(?:\.{NAME})*'


def prefix(package, name):
    return package + '.' + name if package else name


def enclosing_names(fqn, package):
    parts = fqn.removeprefix(package + '.' if package else '').split('.')
    return [prefix(package, '.'.join(parts[:i])) for i in range(1, len(parts)+1)]


def render(data, target, dependencies, limits):
    types, _, refs, _ = data
    # Scope contains top-level classes. A failed API extraction can still have a valid package.
    package = types.get(target, {}).get('package', target.rpartition('.')[0])
    candidates = [target, *refs[target]['source-import']]
    for dep in dependencies:
        candidates.extend([dep, *refs[dep]['signature-type']])

    lines = [HEADING, f'package {package};' if package else UNNAMED, NESTED]
    bindings = {}
    included, omitted, inaccessible = [], [], []
    for fqn in dict.fromkeys(candidates):
        pkg = types.get(fqn, {}).get('package', package)
        owners = enclosing_names(fqn, pkg)
        if fqn != target and (fqn not in types or any(
            owner not in types or 'private' in types[owner]['modifiers'].split()
            or (pkg != package and 'public' not in types[owner]['modifiers'].split())
            for owner in owners
        )):
            inaccessible.append(fqn)
            continue
        outer = owners[0]
        simple = outer.rsplit('.', 1)[-1]
        bound = bindings.get(simple)
        proposed = []
        if bound is not None and bound != outer:
            proposed.append(f'// Use {fqn}.')
        else:
            if pkg and bound is None:
                proposed.append(f'import {outer};')
            if len(owners) > 1 or not pkg:
                spelling = fqn.removeprefix(pkg+'.') if pkg else fqn
                proposed.append(f'// Use {spelling}.')
        # Count unique project types, including the target, and every physical line.
        if (omitted or len(included) >= limits['max_types']
                or len(lines) + len(proposed) + len(LIBRARIES) > limits['max_lines']):
            if fqn == target:
                raise ValueError('Import hints exceed max_lines before optional project types')
            omitted.append(fqn)
            continue
        bindings.setdefault(simple, outer)
        lines.extend(proposed)
        included.append(fqn)
    lines.extend(library_lines(bindings))
    return '\n'.join(lines)+'\n', dict(class_name=target, package=package, included=included,
                                        omitted=omitted, inaccessible=inaccessible, lines=len(lines),
                                        limits=dict(types=limits['max_types'], lines=limits['max_lines']))


def library_lines(bindings):
    for line in LIBRARIES:
        name = line.removeprefix('import ').removesuffix(';')
        simple = name.rsplit('.', 1)[-1]
        if not name.startswith('static ') and bindings.get(simple, name) != name:
            yield f'// Use {name}.'
        else:
            yield line


def gate(block, metadata, limits):
    """Validate the exact block sent to the model before starting any requests."""
    lines = block.splitlines()
    package = metadata['package']
    if package and not re.fullmatch(QUALIFIED, package):
        raise ValueError(f'Invalid import hints package: {package}')
    if lines[:3] != [HEADING, f'package {package};' if package else UNNAMED, NESTED]:
        raise ValueError('Incomplete import hints block')
    if len(lines) > limits['max_lines'] or len(metadata['included']) > limits['max_types']:
        raise ValueError('Import hints exceed dependency limits')
    imports = {}
    covered = set()
    for line in lines[3:-len(LIBRARIES)]:
        use = re.fullmatch(rf'// Use ({QUALIFIED})\.', line)
        if use:
            name = use[1]
            first, dot, rest = name.partition('.')
            covered.add(imports.get(first, first) + dot + rest)
            if not package:
                imports.setdefault(first, first)
            continue
        match = re.fullmatch(rf'import ({QUALIFIED}\.{NAME});', line)
        if not match:
            raise ValueError(f'Malformed import hint: {line}')
        simple = match[1].rsplit('.', 1)[1]
        if simple in imports:
            raise ValueError(f'Conflicting import hints for {simple}')
        imports[simple] = match[1]
        covered.add(match[1])
    if lines[-len(LIBRARIES):] != list(library_lines(imports)):
        raise ValueError('Incomplete test library import hints')
    if metadata['class_name'] not in covered or not covered.issuperset(metadata['included']):
        raise ValueError('Missing project type in import hints')
