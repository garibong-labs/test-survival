"""Compile frozen source snapshots, including helpers loaded only at runtime."""
from pathlib import Path


def compile_snapshot(w, root, selected, part, cp, folder, *, compiler=None, full_suite=False, whole=None):
    """Prefer the whole snapshot; isolate live tests if unrelated tests broke.

    Explicitly compiling non-test helper sources preserves reflection, external
    method providers, and service-loaded classes that javac cannot infer from a
    reference in the selected test. All attempts remain in the job evidence.
    """
    part.mkdir(parents=True, exist_ok=True)
    sources = sorted(root.rglob('*.java'))

    def compile_sources(sources, label):
        classes = part/label; classes.mkdir(exist_ok=True)
        log = part/(label+'.txt')
        if compiler is not None:
            result = compiler.compile(sources, root, cp, classes, folder, log, w.JVM['javac'], w.TIMEOUTS['compile'])
        else:
            args = part/(label+'.args')
            args.write_text('\n'.join('"'+str(p).replace('\\','\\\\').replace('"','\\"')+'"' for p in sources))
            result = w.execute(['javac', '-J-Xmx'+w.JVM['javac'], '-cp', cp, '-sourcepath', str(root),
                                '-d', str(classes), '@'+str(args)], folder, log, w.TIMEOUTS['compile'])
        return classes, {**result, 'source_count':len(sources)}

    # A caller batching multiple files from this root can reuse even a failed
    # whole-tree attempt; per-file fallback must not recompile the suite.
    classes, whole = whole if whole is not None else compile_sources(sources, 'whole-snapshot')
    if whole['exit_code'] == 0 or full_suite:
        return classes, {**whole, 'snapshot_full_suite':True}
    inventory = part/'inventory.tsv'
    # Lenient: a source that does not parse is reported as a "D" row (source,
    # line, message) and skipped; it can never be a helper. Pool roots are
    # filtered of unscreened sources before this point, so this is a safety net.
    parsed = w.execute(['java', '-Xmx'+w.JVM['javac'], str(w.HELPERS/'SourceInventory.java'), str(root), 'auto', 'lenient'],
                       folder, inventory, w.TIMEOUTS['compile'])
    if parsed['exit_code']:
        raise RuntimeError('Cannot inventory snapshot helpers; see '+str(inventory))
    class_files = {}; test_classes = set(); unparsable = set()
    for line in inventory.read_text().splitlines():
        fields = line.split('\t')
        if fields[0] == 'C':
            class_files[fields[1]] = Path(fields[5])
        elif fields[0] == 'M' and fields[3]:
            test_classes.add(fields[1])
        elif fields[0] == 'D':
            unparsable.add(Path(fields[1]).resolve())
    test_files = {class_files[c] for c in test_classes}
    # javac reports absolute paths; compare resolved paths and only relativise
    # sources known to be under root.
    skipped = [p for p in sources if p.resolve() in unparsable]
    helpers = [p for p in sources if p not in test_files and p not in skipped]
    classes, result = compile_sources(sorted(set([*helpers, *selected])), 'selected-and-helpers')
    return classes, {**result, 'snapshot_full_suite':False, 'whole_snapshot_compile':whole,
                     'helper_sources':[str(p.relative_to(root)) for p in helpers],
                     'unparsable_sources':[str(p.relative_to(root)) for p in skipped]}


def filtered_roots(exp, methods, work, excluded=()):
    """Copy source roots to scratch, excluding screened-out tests before parsing.

    Pool manifests and their source hashes stay untouched. Unscreened sources
    (helper classes/providers) remain available to javac and SourceInventory.
    """
    import shutil
    excluded = {(exp/p).resolve() for p in excluded}
    roots = {}
    result = []
    for test in methods:
        root = exp/test.get('source_root', str(Path(test['file']).parent))
        if root not in roots:
            target = work/str(len(roots))
            target.mkdir(parents=True, exist_ok=True)
            for source in root.rglob('*.java'):
                if source.resolve() not in excluded:
                    dest = target/source.relative_to(root)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
            roots[root] = target
        target = roots[root]
        result.append({**test, 'file':str((target/(exp/test['file']).relative_to(root)).relative_to(exp)),
                       'source_root':str(target.relative_to(exp))})
    return result
