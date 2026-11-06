"""Expand syntax inventory into concrete inherited-test identities.

Resolution stays within inventoried sources. Unknown external superclasses are
left unmapped; actual build reports remain authoritative for build discovery.
"""
import re


JAVA_LANG_TYPES = frozenset('''Appendable AutoCloseable Boolean Byte Character CharSequence
Class ClassLoader ClassValue Cloneable Comparable Double Enum Error Exception Float
IllegalArgumentException IllegalStateException Integer Iterable Long Math Module Number
Object Package Process ProcessBuilder Readable Record Runnable Runtime RuntimeException
Short StackTraceElement String StringBuffer StringBuilder System Thread ThreadLocal
Throwable Void'''.split())


def erased_type(name):
    while '<' in name:
        erased = re.sub(r'<[^<>]*>', '', name)
        if erased == name: break
        name = erased
    return name.strip()


def type_arguments(name):
    """Split a source type's arguments without splitting nested generic types."""
    if '<' not in name: return []
    text = name[name.index('<')+1:name.rfind('>')]
    result, start, depth = [], 0, 0
    for i, char in enumerate(text):
        if char == '<': depth += 1
        elif char == '>': depth -= 1
        elif char == ',' and depth == 0:
            result.append(text[start:i].strip()); start = i+1
    return [*result, text[start:].strip()]


def expand(inventory, framework='junit5'):
    classes = inventory['classes']
    declared = {name: [] for name in classes}
    for method in inventory['methods']:
        declared[method['class']].append(method)

    def resolve(name, owner):
        name = erased_type(name)
        if not name: return None
        data = classes[owner]
        candidates = [name] if '.' in name else []
        outer = owner
        while True:
            candidates.append(outer+'$'+name)
            if '$' not in outer: break
            outer = outer.rsplit('$', 1)[0]
        wildcard = []
        for entry in data.get('imports', '').split(';'):
            if entry.endswith('.*'): wildcard.append(entry[:-1]+name)
            elif entry.rsplit('.', 1)[-1] == name.split('.', 1)[0]:
                candidates.append(entry+name[len(name.split('.', 1)[0]):])
        candidates.append((data['package']+'.' if data.get('package') else '')+name)
        candidates.extend(wildcard)
        for candidate in candidates:
            if candidate in classes: return candidate
            # Nested source names use binary '$' separators in the inventory.
            for cls in classes:
                if cls.replace('$', '.') == candidate: return cls
        return None

    def parameter(name, owner, bindings):
        data = classes[owner]
        imports = data.get('imports', '').split(';')
        name = re.sub(r'\s+', '', erased_type(name)).replace('...', '[]')
        base = name.rstrip('[]')
        suffix = name[len(base):]
        if base in bindings: return bindings[base]+suffix
        resolved = resolve(base, owner)
        if resolved: return resolved.replace('$', '.')+suffix
        first = base.split('.', 1)[0]
        for entry in imports:
            if entry.rsplit('.', 1)[-1] == first:
                return (entry+base[len(first):]).replace('$', '.')+suffix
        if first in JAVA_LANG_TYPES: return 'java.lang.'+base+suffix
        if '.' not in base and base not in ('boolean','byte','char','double','float','int','long','short'):
            wildcards = [entry[:-1] for entry in imports if entry.endswith('.*')]
            if len(wildcards) == 1: return wildcards[0]+base+suffix
            if not wildcards: return data.get('package', '')+'.'+base+suffix
            # Multiple unresolved on-demand imports cannot identify a type.
            return owner+':'+base+suffix
        return base.replace('$', '.')+suffix

    def type_parameters(data):
        result = {}
        for value in data.get('type_parameters', '').split(';'):
            if not value.strip(): continue
            parts = re.split(r'\s+extends\s+', value.strip(), maxsplit=1)
            result[parts[0]] = parts[1].split('&', 1)[0] if len(parts) > 1 else 'java.lang.Object'
        return result

    def defaults(data, owner, bindings):
        bounds = type_parameters(data)
        result = dict(bindings)
        # Type variables declared here shadow any outer variable of the same name.
        for name in bounds: result.pop(name, None)
        def bind(name, visiting=frozenset()):
            if name in result: return result[name]
            if name in visiting: return 'java.lang.Object'
            bound = erased_type(bounds[name]).strip()
            if bound in bounds:
                value = bind(bound, visiting | {name})
            else:
                value = parameter(bound, owner, result)
            result[name] = value
            return value
        for name in bounds: bind(name)
        return result

    def signature(method, bindings):
        owner = method['class']
        bindings = defaults(method, owner, bindings)
        name, parameters = erased_type(method['signature']).split('(', 1)
        return name, tuple(parameter(p, owner, bindings) for p in parameters.rstrip(')').split(',') if p.strip())

    memo = {}
    def members(owner, bindings, visiting=frozenset()):
        key = owner, tuple(sorted(bindings.items()))
        if key in memo: return memo[key]
        if owner in visiting: return []
        own = [dict(m, _signature=signature(m, bindings)) for m in declared[owner]]
        # Vintage discovers annotated superclass methods even if a concrete
        # override is unannotated; reflection invokes that overriding body.
        signatures = {m['_signature'] for m in own if framework != 'junit4' or m['kind'] or
                      re.search(r'@(?:org\.junit\.)?Test\b', m.get('annotations', ''))}
        result = list(own)
        data = classes[owner]
        parents = [data.get('extends', ''), *data.get('interfaces', '').split(';')]
        for name in parents:
            parent = resolve(name, owner)
            if parent is None: continue
            parent_bindings = defaults(classes[parent], parent, {})
            for variable, argument in zip(type_parameters(classes[parent]), type_arguments(name)):
                parent_bindings[variable] = parameter(argument, owner, bindings)
            for method in members(parent, parent_bindings, visiting | {owner}):
                if method['_signature'] in signatures: continue
                flags = method.get('modifiers', '')
                if 'private' in flags or 'static' in flags: continue
                declaration = method.get('declaring_class', method['class'])
                # Jupiter discovers non-private superclass tests reflectively,
                # even when Java source access would stop at a package boundary.
                if (framework == 'junit4' and not any(f in flags for f in ('public', 'protected', 'default'))
                        and classes[declaration]['kind'] != 'INTERFACE'
                        and classes[declaration].get('package') != data.get('package')):
                    continue
                signatures.add(method['_signature'])
                result.append(dict(method, **{'class': owner}, declaring_class=declaration,
                                   declaring_file=classes[declaration]['path'], inherited=True,
                                   annotations=method.get('annotations', '')+' '+data.get('annotations', '')))
        memo[key] = result
        return result

    inherited = []
    for owner, data in classes.items():
        if data['kind'] != 'CLASS' or 'abstract' in data.get('modifiers', ''): continue
        inherited.extend({k:v for k,v in m.items() if k != '_signature'}
                         for m in members(owner, defaults(data, owner, {})) if m.get('inherited') and m['kind'])
    return {**inventory, 'methods': [dict(m, file=classes[m['class']]['path'])
                                    for m in inventory['methods']+inherited]}
