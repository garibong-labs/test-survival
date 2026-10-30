"""Container-side batched coverage. Tests execute in exactly one JVM per job."""
import base64
from collections import defaultdict
from pathlib import Path
import shutil

from .snapshot_compile import compile_snapshot, filtered_roots


COVERAGE_VERSION = 2  # Compile and load each source snapshot independently.


def run(w, job):
    work = w.EXP/job['work']
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    original = job['methods']
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
    groups = defaultdict(list)
    for t in job['methods']:
        root = t.get('source_root', str(Path(t['file']).parent))
        groups[root, 'suite' if job.get('full_suite') else t.get('execution_hash', t['file_hash'])].append(t)
    manifest = []; outputs = []; compile_runs = []; snapshots = {}; rejected = []
    for number, tests in enumerate(groups.values()):
        part = work/str(number); part.mkdir()
        root = w.EXP/tests[0].get('source_root', str(Path(tests[0]['file']).parent))
        if root not in snapshots:
            snapshots[root] = compile_snapshot(w, root, [], work/'snapshots'/str(len(snapshots)), cp, folder,
                                               full_suite=True)
        classes, compiled = snapshots[root]
        if compiled['exit_code'] and not job.get('full_suite'):
            classes, compiled = compile_snapshot(w, root, [w.EXP/tests[0]['file']], part, cp, folder,
                                                  whole=snapshots[root])
        compile_runs.append(compiled)
        if compiled['exit_code']:
            if not job.get('survival'):
                raise RuntimeError('Coverage snapshot compilation failed; see '+compiled['log'])
            rejected.extend(dict(t, state='compile-fail', message=(w.EXP/compiled['log']).read_text(errors='replace')[:4000], lines=[]) for t in tests)
            continue
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
    if job.get('survival'):
        scope_file = work/'scope-files.txt'
        scope_file.write_text('\n'.join(job['scope_files'])+'\n')
        scope_options = ['-Dsurvival.coverage.scope='+str(scope_file)]
    run = w.execute(['java', '-Xmx'+w.JVM['jacoco'], *scope_options, f'-javaagent:{agent}=output=none,dumponexit=false', '-cp', str(helper)+':'+helper_cp,
                     'PerTestCoverage', str(manifest_path)], runtime_dir, work/'console.txt',
                    w.TIMEOUTS['test_class']*max(1, len(job['methods'])))
    if run['exit_code'] or run.get('timeout'):
        if job.get('survival'):
            return fallback(w, job, original, work, run)
        raise RuntimeError('Per-test coverage JVM failed; see '+str(work/'console.txt'))
    methods = rejected; universe = set()
    scope = set(job.get('scope_files', []))
    for output, tests in outputs:
        observed = defaultdict(list); covered = defaultdict(set)
        path = output/'tests.tsv'
        for line in path.read_text().splitlines() if path.exists() else []:
            serial, cls, name, state, message = line.split('\t')
            ident = (cls, name)
            observed[ident].append((state, base64.b64decode(message).decode()))
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
    w.write_json(w.EXP/job['output'], dict(coverage_status='complete', coverage_version=COVERAGE_VERSION, status='evaluated', index=job['index'], sha=job['sha'], methods=methods,
                                         universe=sorted(universe), run=run, compile=compile_runs))


def fallback(w, job, original, work, failure):
    """A failed batch JVM loses coverage, never the index's survival evidence."""
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
    w.write_json(w.EXP/job['output'], dict(index=job['index'], sha=job['sha'], status='evaluated',
                                         coverage_version=COVERAGE_VERSION, coverage_status='missing',
                                         coverage_reason='batch JVM failed or timed out',
                                         methods=methods, files=files, run=failure))
