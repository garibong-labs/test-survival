"""Compact per-method dependencies; no production line coverage is persisted."""
from collections import defaultdict
from pathlib import Path
import re
import struct

RECORD_VERSION = 2  # Includes production hits from pre-plan test discovery.


class Reader:
    def __init__(self, data): self.data, self.at = data, 0
    def take(self, n):
        value = self.data[self.at:self.at+n]; self.at += n
        if len(value) != n: raise ValueError('Truncated class file')
        return value
    def u1(self): return self.take(1)[0]
    def u2(self): return struct.unpack('>H', self.take(2))[0]
    def u4(self): return struct.unpack('>I', self.take(4))[0]


def class_info(path):
    r = Reader(path.read_bytes())
    if r.u4() != 0xcafebabe: raise ValueError('Invalid class file')
    r.take(4); size = r.u2(); pool = [None]*size; names = []; i = 1
    while i < size:
        tag = r.u1()
        if tag == 1: pool[i] = r.take(r.u2()).decode('utf-8', errors='replace')
        elif tag == 7: pool[i] = r.u2(); names.append(i)
        elif tag in (8,16,19,20): r.take(2)
        elif tag in (3,4,9,10,11,12,17,18): r.take(4)
        elif tag in (5,6): r.take(8); i += 1
        elif tag == 15: r.take(3)
        else: raise ValueError('Unsupported constant tag '+str(tag))
        i += 1
    r.u2(); own = pool[pool[r.u2()]]; r.u2()
    r.take(2*r.u2())
    def attributes():
        result = {}
        for _ in range(r.u2()):
            name = pool[r.u2()]; result[name] = r.take(r.u4())
        return result
    for _ in range(2):
        for _ in range(r.u2()): r.take(6); attributes()
    attrs = attributes()
    source = pool[struct.unpack('>H', attrs['SourceFile'])[0]] if 'SourceFile' in attrs else None
    refs = {pool[pool[n]] for n in names}
    for value in pool:
        if isinstance(value, str): refs.update(re.findall(r'L([\w$/]+)(?=[;<])', value))
    refs = {name for value in refs for name in (re.findall(r'L([\w$/]+);', value) if value.startswith('[') else [value])}
    return dict(name=own, source=source, references=refs-{own})


def class_inventory(root):
    return {item['name']:item for p in sorted(root.rglob('*.class')) if 'META-INF' not in p.relative_to(root).parts
            for item in [class_info(p)]}


def source_inventory(w, root, folder, work, label, helper_classes=None):
    output = work/(label+'-sources.tsv')
    entry = ['-cp', str(helper_classes), 'DependencySources'] if helper_classes else [str(w.HELPERS/'DependencySources.java')]
    result = w.execute(['java', '-Xmx'+w.JVM['javac'], *entry, str(root)],
                       folder, output, w.TIMEOUTS['compile'])
    if result['exit_code'] or result.get('timeout'): raise ValueError('Cannot parse dependency sources')
    names = defaultdict(set); identifiers = defaultdict(set)
    for line in output.read_text().splitlines():
        fields = line.split('\t')
        if len(fields) == 3:
            kind, file, name = fields
            (names if kind == 'C' else identifiers)[file].add(name)
    return names, identifiers


class Dependencies:
    def __init__(self, w, folder, work, helper_classes=None):
        self.w, self.folder, self.work = w, folder, work
        self.helper_classes = helper_classes
        self.production = class_inventory(folder/'target/classes')
        declarations, _ = source_inventory(w, folder/'src/main/java', folder, work, 'production', helper_classes)
        self.by_simple = defaultdict(set)
        for file, names in declarations.items():
            for name in names: self.by_simple[name].add('src/main/java/'+file)
        self.files = {}
        for name, info in self.production.items():
            package = name.rpartition('/')[0]
            source = (package+'/' if package else '')+(info['source'] or '')
            if source in declarations: self.files[name] = 'src/main/java/'+source
        self.snapshots = {}

    def snapshot(self, classes, root):
        if classes not in self.snapshots:
            graph = class_inventory(classes)
            _, identifiers = source_inventory(self.w, root, self.folder, self.work, 'test-'+str(len(self.snapshots)), self.helper_classes)
            self.snapshots[classes] = (graph, identifiers)
        return self.snapshots[classes]

    def record(self, test, classes, root, runtime):
        graph, identifiers = self.snapshot(classes, root)
        start = test['class'].replace('.', '/')
        pending = [n for n in graph if n == start or n.startswith(start+'$')]
        visited = set(); production = set(runtime); sources = set(); unbounded = not pending
        while pending:
            name = pending.pop()
            if name in visited: continue
            visited.add(name); item = graph[name]
            if item['source']:
                package = name.rpartition('/')[0]
                source = (package+'/' if package else '')+item['source']
                if not (root/source).is_file():
                    candidates = list(root.rglob(item['source']))
                    if len(candidates) == 1: source = str(candidates[0].relative_to(root))
                    else: unbounded = True
                sources.add(source)
            else: unbounded = True
            pending.extend(n for n in graph if n.startswith(name+'$') and n not in visited)
            for ref in item['references']:
                if ref in graph: pending.append(ref)
                elif ref in self.production: production.add(ref)
                elif ref.split('/')[-1].split('$')[0] in self.by_simple:
                    unbounded = True
        files = {self.files[n] for n in production if n in self.files}
        unbounded |= any(n not in self.files for n in production)
        # The selected file is required even when its class's SourceFile is absent.
        sources.add(str(Path(test['file']).relative_to(root)))
        for source in sources:
            if source not in identifiers:
                # A source with no identifiers is legitimate; a missing source is not.
                unbounded |= not (root/source).is_file()
            for name in identifiers.get(source, ()):
                files.update(self.by_simple.get(name, ()))
        return dict(version=RECORD_VERSION, selection='affected', files=sorted(files), unbounded=bool(unbounded))
