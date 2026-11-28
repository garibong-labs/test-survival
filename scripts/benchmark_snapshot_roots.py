"""Time the affected-selection worker on N one-file JUnit roots.

Run with a JDK and the Java fixture jars, for example in the fixture container:

docker run --rm --network none -e PYTHONPATH=/src -v "$PWD/src:/src:ro" \
    -v "$PWD/scripts:/scripts:ro" -v "$SURVIVAL_JAVA_TEST_LIBS:/libs:ro" \
    "$SURVIVAL_JAVA_TEST_IMAGE" python3 /scripts/benchmark_snapshot_roots.py --libs /libs --roots 16
"""
import argparse
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter

from test_survival.adapters import java_worker as worker, policy_coverage


def benchmark(folder, libs, count):
    worker.EXP = folder
    worker.HELPERS = Path(worker.__file__).parent/'java'
    worker.JVM = dict(javac='256m', jacoco='256m')
    worker.TIMEOUTS = dict(compile=60, test_class=60)
    (folder/'work').mkdir()
    (folder/'work/libs').symlink_to(libs.resolve(), target_is_directory=True)
    main = folder/'src/main/java/sample/Value.java'
    main.parent.mkdir(parents=True)
    main.write_text('package sample; public class Value { public static int get() { return 1; } }')
    classes = folder/'target/classes'
    classes.mkdir(parents=True)
    subprocess.run(['javac', '-g', '-d', str(classes), str(main)], check=True)
    records = json.loads((worker.HELPERS/'classpath-artifacts.json').read_text())
    cp = ':'.join([str(classes), *(str(libs/name) for name in worker.runner_test_jars(records))])
    worker.classpath = lambda _: (folder, cp)
    methods = []
    for i in range(count):
        root = f'tests/{i}'
        source = folder/root/'ValueTest.java'
        source.parent.mkdir(parents=True)
        source.write_text('class ValueTest { @org.junit.jupiter.api.Test void value() { '
                          'org.junit.jupiter.api.Assertions.assertEquals(1,sample.Value.get()); } }')
        methods.append(dict(id=str(i), file=str(source.relative_to(folder)), source_root=root,
                            file_hash=str(i), method='value', **{'class': 'ValueTest'}))
    job = dict(index=0, sha='fixture', selection='affected', survival=True, methods=methods,
               scope_files=[str(main.relative_to(folder))], scope_classes=['sample.Value'],
               work='job', output='measurement.json')
    for workers in (1, 8):
        start = perf_counter()
        policy_coverage.run(worker, dict(job, chunk_workers=workers))
        elapsed = perf_counter()-start
        rows = json.loads((folder/job['output']).read_text())['methods']
        if len(rows) != count or any(row['state'] != 'pass' or row['dependency_record']['unbounded']
                                     for row in rows):
            raise RuntimeError('The benchmark fixture did not produce the expected bounded passes')
        print(f'{count} roots, chunk_workers={workers}: {elapsed:.3f} s', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roots', type=int, default=16)
    parser.add_argument('--libs', type=Path, required=True, help='Directory of Java test fixture jars')
    args = parser.parse_args()
    if args.roots < 1:
        parser.error('--roots must be positive')
    with TemporaryDirectory() as directory:
        benchmark(Path(directory), args.libs.resolve(), args.roots)
