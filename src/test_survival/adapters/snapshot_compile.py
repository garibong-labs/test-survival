"""Compile frozen snapshots, removing only sources with javac error diagnostics."""
import base64
from pathlib import Path
import shutil


STRATEGY = 'remove-error-files-v1'


def test_source_files(w, root, folder, part, helper_classes=None):
    """Inventory test declarations without resolving their production references."""
    inventory = part/'inventory.tsv'
    entry = ['-cp', str(helper_classes), 'SourceInventory'] if helper_classes else [str(w.HELPERS/'SourceInventory.java')]
    parsed = w.execute(['java', '-Xmx'+w.JVM['javac'], *entry, str(root), 'auto'],
                       folder, inventory, w.TIMEOUTS['compile'])
    # Syntax errors still produce partial inventory. Include every discovered test
    # and let the diagnostic compiler classify broken sources, rather than aborting.
    classes = {}; tests = set()
    for line in inventory.read_text().splitlines():
        fields = line.split('\t')
        if fields[0] == 'C' and len(fields) == 6:
            classes[fields[1]] = Path(fields[5]).resolve()
        elif fields[0] == 'M' and len(fields) > 3 and fields[3]:
            tests.add(fields[1])
    if parsed.get('timeout') or parsed['exit_code'] and not classes:
        raise RuntimeError('Cannot inventory snapshot: '+str(inventory))
    return {classes[c] for c in tests if c in classes}


def compile_snapshot(w, root, selected, part, cp, folder, *, compiler=None, full_suite=False, whole=None, selected_only=False, helper_classes=None):
    """One retry sequence per root. Failed outputs are never supplied to a JVM.

    selected_only retains non-test helpers, and javac may discover referenced
    test sources from the scratch sourcepath. All removals are scratch-only.
    """
    if whole is not None:
        return whole
    root = root.resolve()
    part.mkdir(parents=True, exist_ok=True)
    scratch = part/'sources'
    shutil.copytree(root, scratch)
    all_sources = sorted(root.rglob('*.java'))
    chosen = set(all_sources)
    if selected_only:
        chosen = (chosen - test_source_files(w, root, folder, part, helper_classes)) | {p.resolve() for p in selected}
    removed = {}; attempts = []; classes = part/'classes-0'
    while chosen:
        number = len(attempts)
        classes = part/f'classes-{number}'; classes.mkdir()
        args = part/f'compile-{number}.sources'
        args.write_text('\n'.join(str(scratch/p.relative_to(root)) for p in sorted(chosen))+'\n')
        diagnostic_path = part/f'compile-{number}.errors.tsv'
        log = part/f'compile-{number}.txt'
        entry = ['-cp', str(helper_classes), 'SnapshotCompiler'] if helper_classes else [str(w.HELPERS/'SnapshotCompiler.java')]
        command = ['java', '-Xmx'+w.JVM['javac'], *entry, str(args), str(diagnostic_path),
                   '-Xmaxerrs', '2147483647', '-cp', cp, '-sourcepath', str(scratch), '-d', str(classes)]
        result = w.execute(command, folder, log, w.TIMEOUTS['compile'])
        errors = {}
        for line in diagnostic_path.read_text().splitlines() if diagnostic_path.exists() else []:
            source, message = [base64.b64decode(v).decode() for v in line.split('\t')]
            if not source or not Path(source).is_relative_to(scratch):
                raise RuntimeError('Unmapped compiler diagnostic; see '+str(log))
            original = root/Path(source).relative_to(scratch)
            errors.setdefault(original, []).append(message)
        attempts.append({**result, 'sources':[str(p.relative_to(root)) for p in sorted(chosen)],
                         'removed': {str(p.relative_to(root)): ds for p, ds in errors.items()}})
        if result['exit_code'] == 0 and not result.get('timeout'):
            break
        shutil.rmtree(classes)
        if result.get('timeout') or not errors or any(not (scratch/p.relative_to(root)).exists() for p in errors):
            raise RuntimeError('Snapshot compilation failed without removable source errors; see '+str(log))
        for source, messages in errors.items():
            removed[str(source.relative_to(root))] = messages
            (scratch/source.relative_to(root)).unlink()
            chosen.discard(source)
    if not chosen:
        classes.mkdir(exist_ok=True)
    last = attempts[-1] if attempts else dict(exit_code=0, log='', seconds=0)
    return classes, {**last, 'exit_code':0 if chosen else 1, 'strategy':STRATEGY,
                     'snapshot_full_suite':not removed and not selected_only,
                     'source_count':len(chosen), 'removed':removed, 'attempts':attempts,
                     'source_root':str(root), 'compiled_source_root':str(scratch)}


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
