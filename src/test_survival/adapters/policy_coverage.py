"""Container-side batched coverage. Tests execute in exactly one JVM per job."""
import base64
from collections import defaultdict
from pathlib import Path
import shutil


def run(w, job):
    work = w.EXP/job['work']
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
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
        groups['suite' if job.get('full_suite') else t['file_hash']].append(t)
    manifest = []; outputs = []; compile_runs = []
    for number, tests in enumerate(groups.values()):
        part = work/str(number); part.mkdir()
        classes = part/'classes'; classes.mkdir()
        root = w.EXP/tests[0].get('source_root', str(Path(tests[0]['file']).parent))
        sources = sorted(root.rglob('*.java')) if job.get('full_suite') else [w.EXP/tests[0]['file']]
        args = part/'sources.args'
        args.write_text('\n'.join('"'+str(p).replace('\\', '\\\\').replace('"', '\\"')+'"' for p in sources))
        compiled = w.execute(['javac', '-J-Xmx'+w.JVM['javac'], '-cp', cp, '-sourcepath', str(root), '-d', str(classes), '@'+str(args)], folder, part/'javac.txt', w.TIMEOUTS['compile'])
        compile_runs.append(compiled)
        if compiled['exit_code']:
            raise RuntimeError('Coverage suite compilation failed; see '+str(part/'javac.txt'))
        selection = part/'selectors.tsv'
        selection.write_text('\n'.join(sorted({t['class'] if job.get('full_suite') else t['class']+'\t'+t['method'] for t in tests}))+'\n')
        resource_root = w.EXP/tests[0].get('resource_root', 'dev/src-test-t')
        runtime = ':'.join(map(str, [classes, helper, helper_cp, resource_root/'resources', resource_root/'suite']))
        output = part/'dumps'
        outputs.append((output, tests))
        manifest.append('\t'.join(map(str, [output, folder/'target/classes', selection, runtime])))
    manifest_path = work/'batches.tsv'; manifest_path.write_text('\n'.join(manifest)+'\n' if manifest else '')
    runtime_dir = work/'runtime'; runtime_dir.mkdir()
    if job['methods']:
        resource_root = w.EXP/job['methods'][0].get('resource_root', 'dev/src-test-t')
        for name in ('resources', 'suite'):
            if (resource_root/name).exists():
                shutil.copytree(resource_root/name, runtime_dir/'src/test'/name)
    run = w.execute(['java', '-Xmx'+w.JVM['jacoco'], f'-javaagent:{agent}=output=none,dumponexit=false', '-cp', str(helper)+':'+helper_cp,
                     'PerTestCoverage', str(manifest_path)], runtime_dir, work/'console.txt',
                    w.TIMEOUTS['test_class']*max(1, len(job['methods'])))
    if run['exit_code']:
        raise RuntimeError('Per-test coverage JVM failed; see '+str(work/'console.txt'))
    methods = []; universe = set()
    for output, tests in outputs:
        observed = defaultdict(list); covered = defaultdict(set)
        path = output/'tests.tsv'
        for line in path.read_text().splitlines() if path.exists() else []:
            serial, cls, name, state, message = line.split('\t')
            ident = (cls, name)
            observed[ident].append((state, base64.b64decode(message).decode()))
            for item in (output/(serial+'.tsv')).read_text().splitlines():
                file, lineno, hit = item.split('\t'); coordinate = (file, int(lineno))
                universe.add(coordinate)
                if hit == 'true': covered[ident].add(coordinate)
        for t in tests:
            ident = (t['class'], t['method']); states = observed[ident]
            state = 'pass' if states and all(s == 'SUCCESSFUL' for s, _ in states) else 'error' if states else 'absent'
            methods.append(dict(id=t['id'], file_hash=t['file_hash'], **{'class':t['class']}, method=t['method'], state=state,
                                message='\n'.join(m for _, m in states), lines=sorted(covered[ident])))
    w.write_json(w.EXP/job['output'], dict(status='evaluated', index=job['index'], sha=job['sha'], methods=methods,
                                         universe=sorted(universe), run=run, compile=compile_runs))
