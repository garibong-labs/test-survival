"""Container-side coverage with bounded, sequential snapshot batches."""
import base64
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil

from .snapshot_compile import compile_snapshot, filtered_roots


COVERAGE_VERSION = 3  # Retry legacy missing records with bounded instrumentation.
MAX_SNAPSHOT_GROUPS = 8


def includes_pattern(classes):
    """JaCoCo package patterns for the production scope (including nested types)."""
    patterns = set()
    for cls in classes:
        package, _, name = cls.rpartition('.')
        if package:
            patterns.add(package+'.*')
        else:
            # A bare '*' would also instrument every dependency package.
            patterns.update((name, name+'$*'))
    return ':'.join(sorted(patterns))


def run(w, job):
    work = w.EXP/job['work']
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    original = job['methods']
    dependencies = None
    dependency_error = ''
    if job.get('survival'):
        job = {**job, 'methods': filtered_roots(w.EXP, original, work/'sources', job.get('excluded_files', []))}
    folder, cp = w.classpath(job['index'])
    version = w.jacoco_version(folder/'target/classes')
    agent = w.EXP/f'work/libs/org.jacoco.agent-{version}-runtime.jar'
    cli = w.EXP/f'work/libs/org.jacoco.cli-{version}-nodeps.jar'
    helper = work/'helper'; helper.mkdir()
    helper_cp = cp+':'+str(cli)+':'+str(agent)
    compiled = w.execute(['javac', '-cp', helper_cp, '-d', str(helper), str(w.HELPERS/'PerTestCoverage.java')], folder, work/'helper.txt', w.TIMEOUTS['compile'])
    if compiled['exit_code']:
        raise RuntimeError('Cannot compile per-test coverage launcher')
    if job.get('survival') and job.get('selection') == 'affected':
        from .dependencies import Dependencies
        try:
            dependencies = Dependencies(w, folder, work)
        except (OSError, ValueError, RuntimeError) as exc:
            dependency_error = str(exc)
    dependency_inputs = {}
    groups = defaultdict(list)
    for t in job['methods']:
        root = t.get('source_root', str(Path(t['file']).parent))
        groups[root, 'suite' if job.get('full_suite') else t.get('execution_hash', t['file_hash'])].append(t)
    manifest = []; outputs = []; compile_runs = []; snapshots = {}; rejected = []
    for number, tests in enumerate(groups.values()):
        part = work/str(number); part.mkdir()
        root = w.EXP/tests[0].get('source_root', str(Path(tests[0]['file']).parent))
        if root not in snapshots:
            selected = [w.EXP/t['file'] for t in job['methods']
                        if w.EXP/t.get('source_root', str(Path(t['file']).parent)) == root]
            snapshots[root] = compile_snapshot(w, root, selected, work/'snapshots'/str(len(snapshots)), cp, folder,
                                               full_suite=True, selected_only=job.get('selection') == 'affected')
        classes, compiled = snapshots[root]
        compile_runs.append(compiled)
        retained = []
        for t in tests:
            relative = str((w.EXP/t['file']).relative_to(root))
            diagnostics = compiled.get('removed', {}).get(relative)
            if diagnostics is not None:
                rejected.append(dict(t, state='compile-fail', message='\n'.join(diagnostics)[:4000], lines=[]))
            else:
                retained.append(t)
        tests = retained
        if not tests:
            continue
        if compiled['exit_code']:
            raise RuntimeError('Snapshot failed without diagnostics for selected sources')
        for t in tests:
            dependency_inputs[(t['class'], t['method'], t.get('execution_hash',t['file_hash']))] = (classes, root, w.EXP/t['file'])
        selection = part/'selectors.tsv'
        selection.write_text('\n'.join(sorted({t['class'] if job.get('full_suite') else t['class']+'\t'+t['method'] for t in tests}))+'\n')
        resource_root = w.EXP/tests[0].get('resource_root', 'dev/src-test-t')
        runtime = ':'.join(map(str, [classes, helper, helper_cp, resource_root/'resources', resource_root/'suite']))
        output = part/'dumps'
        outputs.append((output, tests))
        fields = [output, folder/'target/classes', selection, runtime]
        if job.get('survival'):
            fields.append(resource_root)
        manifest.append('\t'.join(map(str, fields)))
    manifest_path = work/'batches.tsv'; manifest_path.write_text('\n'.join(manifest)+'\n' if manifest else '')
    runtime_dir = work/'runtime'; runtime_dir.mkdir()
    if job['methods']:
        resource_root = w.EXP/job['methods'][0].get('resource_root', 'dev/src-test-t')
        for name in ('resources', 'suite'):
            if (resource_root/name).exists():
                shutil.copytree(resource_root/name, runtime_dir/'src/test'/name)
    scope_options = []
    agent_options = 'output=none,dumponexit=false'
    if job.get('survival'):
        scope_file = work/'scope-files.txt'
        scope_file.write_text('\n'.join(job['scope_files'])+'\n')
        scope_options = ['-Dsurvival.coverage.scope='+str(scope_file)]
        if job.get('survival') and job.get('selection') == 'affected':
            scope_options += ['-Dsurvival.dependencies=true']
            from .dependencies import class_inventory
            # Instrument exact production binaries, not third-party packages.
            classes = class_inventory(folder/'target/classes')
            agent_options += ',includes='+(':'.join(n.replace('/', '.') for n in sorted(classes)) or '__no_production__')
        else:
            agent_options += ',includes='+includes_pattern(job['scope_classes'])
    override = os.environ.get('SURVIVAL_JVM_JACOCO')
    heap = override or w.JVM['jacoco']
    jvm = dict(configured=w.JVM['jacoco'], effective=heap,
               environment={'SURVIVAL_JVM_JACOCO': override} if override else {})
    w.write_json(work/'coverage-jvm.json', dict(jvm=jvm, agent_options=agent_options))
    methods = rejected; universe = set(); runs = []; files = []; successful = []
    def launch(start):
        chunk = work/'chunks'/str(start//MAX_SNAPSHOT_GROUPS); chunk.mkdir(parents=True)
        cwd = chunk/'runtime'; cwd.mkdir()
        if (runtime_dir/'src').exists(): shutil.copytree(runtime_dir/'src', cwd/'src')
        chunk_manifest = chunk/'batches.tsv'
        chunk_manifest.write_text('\n'.join(manifest[start:start+MAX_SNAPSHOT_GROUPS])+'\n')
        chunk_outputs = outputs[start:start+MAX_SNAPSHOT_GROUPS]
        tests = [t for _, members in chunk_outputs for t in members]
        memory = chunk/'memory.tsv'
        run = w.execute(['java', '-Xmx'+heap, *scope_options, '-Dsurvival.memory='+str(memory),
                         f'-javaagent:{agent}={agent_options}', '-cp', str(helper)+':'+helper_cp,
                         'PerTestCoverage', str(chunk_manifest)], cwd, chunk/'console.txt',
                        w.TIMEOUTS['test_class']*len(tests))
        measured_memory = {key:int(value) for line in memory.read_text().splitlines() for key,value in [line.split('\t')]} if memory.exists() else {}
        return chunk, chunk_outputs, tests, dict(run, manifest=str(chunk_manifest.relative_to(w.EXP)), groups=len(chunk_outputs), memory=measured_memory)
    with ThreadPoolExecutor(max_workers=job.get('chunk_workers', 1)) as executor:
        launched = executor.map(launch, range(0, len(manifest), MAX_SNAPSHOT_GROUPS))
        for chunk, chunk_outputs, tests, run in launched:
            runs.append(run)
            if run['exit_code'] or run.get('timeout'):
                if not job.get('survival'):
                    raise RuntimeError('Per-test coverage JVM failed; see '+str(chunk/'console.txt'))
                failed = fallback(w, {**job, 'methods':tests}, original, chunk)
                methods.extend(failed['methods']); files.extend(failed['files'])
            else:
                successful.extend(chunk_outputs)
    scope = set(job.get('scope_files', []))
    for output, tests in successful:
        observed = defaultdict(list); covered = defaultdict(set); runtime = defaultdict(set); runtime_missing = set()
        path = output/'tests.tsv'
        for line in path.read_text().splitlines() if path.exists() else []:
            serial, cls, name, state, message = line.split('\t')
            ident = (cls, name)
            observed[ident].append((state, base64.b64decode(message).decode()))
            dep_path = output/(serial+'.classes')
            if dep_path.exists(): runtime[ident].update(dep_path.read_text().splitlines())
            else: runtime_missing.add(ident)
            for item in (output/(serial+'.tsv')).read_text().splitlines():
                file, lineno, hit = item.split('\t'); coordinate = (file, int(lineno))
                if job.get('survival') and file not in scope:
                    continue
                universe.add(coordinate)
                if hit == 'true': covered[ident].add(coordinate)
        for t in tests:
            ident = (t['class'], t['method']); states = observed[ident]
            state = 'pass' if states and all(s == 'SUCCESSFUL' for s, _ in states) else 'fail' if states else 'absent'
            methods.append(dict(id=t['id'], file_hash=t['file_hash'], execution_hash=t.get('execution_hash', t['file_hash']), **{'class':t['class']}, method=t['method'], state=state,
                                message='\n'.join(m for s, m in states if s != 'SUCCESSFUL' and m)[:4000], lines=sorted(covered[ident])))
            if job.get('survival') and job.get('selection') == 'affected':
                record = dict(version=1, selection='affected', files=[], unbounded=True, reason=dependency_error or 'missing execution')
                if dependencies is not None and states and ident not in runtime_missing:
                    try:
                        classes, root, source = dependency_inputs[(t['class'],t['method'],t.get('execution_hash',t['file_hash']))]
                        record = dependencies.record({**t, 'file':str(source)}, classes, root, runtime[ident])
                    except (ValueError, OSError, RuntimeError) as exc: record['reason'] = str(exc)
                methods[-1]['dependency_record'] = record
    if job.get('scope_universe'):
        output = work/'universe.tsv'
        analysis = w.execute(['java', '-Xmx'+heap, '-cp', str(helper)+':'+helper_cp,
                              'PerTestCoverage', '--universe', str(folder/'target/classes'),
                              str(output), str(scope_file)], folder, work/'universe.txt', w.TIMEOUTS['compile'])
        if analysis['exit_code'] or analysis.get('timeout'):
            raise RuntimeError('Cannot enumerate executable scope lines')
        universe = {(f, int(n)) for line in output.read_text().splitlines() for f,n in [line.split('\t')]}
    if job.get('survival') and job.get('selection') == 'affected':
        for t in methods:
            t.setdefault('dependency_record', dict(version=1, selection='affected', files=[], unbounded=True, reason='no instrumented execution'))
    missing = any(r['exit_code'] or r.get('timeout') for r in runs)
    w.write_json(w.EXP/job['output'], dict(coverage_status='missing' if missing else 'complete',
                                         coverage_reason='batch JVM failed or timed out' if missing else '',
                                         coverage_version=COVERAGE_VERSION, status='evaluated', index=job['index'], sha=job['sha'], methods=methods,
                                         universe=sorted(universe), runs=runs, jvm=jvm, files=files, compile=compile_runs))


def fallback(w, job, original, work):
    """A failed chunk loses coverage, never the index's survival evidence."""
    files = []
    by_file = defaultdict(list)
    for t in job['methods']:
        by_file[t['file']].append(t)
    for n, (filename, tests) in enumerate(by_file.items()):
        files.append(w.run_file(job['index'], 'policy-fallback', filename, tests, work/'fallback'/str(n),
                                retry=False, environment_retry=False))
    methods = []
    originals = {(t.get('execution_hash', t['file_hash']), t['class'], t['method']):t for t in original}
    for file in files:
        for t in file['methods']:
            source = originals[(t.get('execution_hash', t['file_hash']), t['class'], t['method'])]
            state = t['state'] if t['state'] in ('pass', 'absent', 'compile-fail') else 'fail'
            methods.append(dict(source, state=state, message=t.get('message', '')[:4000], flaky=False))
            # Pool source metadata may contain anchor coverage, which is not
            # evidence for this failed launch at the current index.
            methods[-1].pop('lines', None)
    return dict(methods=methods, files=files)
