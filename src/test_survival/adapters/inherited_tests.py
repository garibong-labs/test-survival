"""Expand syntax inventory into concrete inherited-test identities.

Resolution stays within inventoried sources. Unknown external superclasses are
left unmapped; actual build reports remain authoritative for build discovery.
"""
import re


def expand(inventory, framework='junit5'):
    classes = inventory['classes']
    declared = {name: [] for name in classes}
    for method in inventory['methods']:
        declared[method['class']].append(method)

    def resolve(name, owner):
        while '<' in name:
            erased = re.sub(r'<[^<>]*>', '', name)
            if erased == name: break
            name = erased
        name = name.strip()
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

    memo = {}
    def members(owner, visiting=frozenset()):
        if owner in memo: return memo[owner]
        if owner in visiting: return []
        own = declared[owner]
        # Vintage discovers annotated superclass methods even if a concrete
        # override is unannotated; reflection invokes that overriding body.
        signatures = {m['signature'] for m in own if framework != 'junit4' or m['kind'] or
                      re.search(r'@(?:org\.junit\.)?Test\b', m.get('annotations', ''))}
        result = list(own)
        data = classes[owner]
        parents = [data.get('extends', ''), *data.get('interfaces', '').split(';')]
        for name in parents:
            parent = resolve(name, owner)
            if parent is None: continue
            for method in members(parent, visiting | {owner}):
                if method['signature'] in signatures: continue
                flags = method.get('modifiers', '')
                if 'private' in flags or 'static' in flags: continue
                declaration = method.get('declaring_class', method['class'])
                # Jupiter discovers non-private superclass tests reflectively,
                # even when Java source access would stop at a package boundary.
                if (framework == 'junit4' and not any(f in flags for f in ('public', 'protected', 'default'))
                        and classes[declaration]['kind'] != 'INTERFACE'
                        and classes[declaration].get('package') != data.get('package')):
                    continue
                signatures.add(method['signature'])
                result.append(dict(method, **{'class': owner}, declaring_class=declaration,
                                   declaring_file=classes[declaration]['path'], inherited=True,
                                   annotations=method.get('annotations', '')+' '+data.get('annotations', '')))
        memo[owner] = result
        return result

    inherited = []
    for owner, data in classes.items():
        if data['kind'] != 'CLASS' or 'abstract' in data.get('modifiers', ''): continue
        inherited.extend(m for m in members(owner) if m.get('inherited') and m['kind'])
    return {**inventory, 'methods': inventory['methods']+inherited}
