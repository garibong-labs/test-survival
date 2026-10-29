"""Three independent compiler sessions, 66 requests each, with a 180s container cap."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, '/code/src')
from test_survival.adapters.compiler_session import CompilerSession


def worker(index):
    root = Path('/experiment')/str(index)
    sourcepath, production, output = root/'source', root/'production', root/'work/classes'
    for path in (sourcepath, production, output):
        path.mkdir(parents=True)
    source = sourcepath/'Sample.java'
    command = ['javac', '-J-Xmx512m', '-cp', str(production), '-sourcepath', str(sourcepath), '-d', str(output), str(source)]

    def execute(args, cwd, log, timeout):
        with log.open('w') as stream:
            result = subprocess.run(args, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
        return {'exit_code': result.returncode}

    def snapshot():
        return {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in output.rglob('*') if p.is_file()}

    def reset():
        shutil.rmtree(output)
        output.mkdir()

    contents = [f'class Sample {{ int value() {{ return {index}; }} }}',
                'class Sample { Missing value; }',
                f'class Sample {{ int value() {{ return {index+10}; }} }}']
    expected = []
    for content in contents:
        source.write_text(content)
        reset()
        result = execute(command, root, root/'cli.txt', 20)
        expected.append((result['exit_code'], snapshot(), (root/'cli.txt').read_bytes()))

    pids, resources, cold = set(), {}, 0
    with CompilerSession(root, execute, 'reused-jvm') as session:
        for iteration in range(66):
            which = iteration % 3
            source.write_text(contents[which])
            reset()
            result = session.execute(command, root, root/'api.txt', 20)
            assert (result['exit_code'], snapshot(), (root/'api.txt').read_bytes()) == expected[which]
            backend = result['compiler_backend']
            assert backend['mode'] == 'reused-jvm'
            pid = backend['pid']
            pids.add(pid)
            cold += backend['cold']
            status = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines())
            rss_kib = int(status['VmRSS'].split()[0])
            fds = len(list(Path(f'/proc/{pid}/fd').iterdir()))
            resources.setdefault(pid, []).append((rss_kib, fds))
        child = session.child
    assert child.poll() is not None
    assert len(pids) == cold == 2  # The real 64-request boundary, not a mocked counter.
    for samples in resources.values():
        assert max(rss for rss, _ in samples) < 768*1024
        assert max(fd for _, fd in samples)-min(fd for _, fd in samples) <= 3
    return {'worker': index, 'pids': sorted(pids), 'compiles': 66,
            'peak_rss_kib': max(rss for samples in resources.values() for rss, _ in samples),
            'fd_ranges': [[min(fd for _, fd in samples), max(fd for _, fd in samples)] for samples in resources.values()]}


started = time.monotonic()
with ThreadPoolExecutor(max_workers=3) as pool:
    results = list(pool.map(worker, range(3)))
assert len({pid for row in results for pid in row['pids']}) == 6
report = {'workers': results, 'seconds': time.monotonic()-started, 'equivalent': True}
Path('/experiment/parallel-report.json').write_text(json.dumps(report, indent=2)+'\n')
print(json.dumps(report))
