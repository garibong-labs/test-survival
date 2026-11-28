"""Time screen progress on synthetic evidence. Run with uv run python scripts/benchmark_progress.py."""
import argparse
from contextlib import redirect_stdout
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import validate
from test_survival.display import UTC_DISPLAY
from test_survival.io import now, write_json
from test_survival.pipeline import log
from test_survival.progress import description


def benchmark(exp, count):
    config = validate({'name': 'progress-benchmark', 'anchor': 't', 'project': {'repo': str(exp)},
                       'generators': [{'name': 'sample', 'method': 'ouedraogo', 'impl': 'native',
                                       'params': {'prompts': ['ZSL']}}]})
    write_json(exp/'state/screen.json', {'status': 'running', 'started': now()})
    directory = exp/'generated/sample/round1'
    sources = directory/'sources/ZSL'
    sources.mkdir(parents=True)
    adapter = JavaMavenAdapter(exp, config)
    provenance = adapter.provenance()
    for index in range(count):
        source = sources/f'Test{index:08d}.java'
        source.write_text(f'class {source.stem} {{}}\n')
        fingerprint = adapter.screen_fingerprint(source, config['screen']['stable_runs'], provenance=provenance)
        write_json(directory/f'validation/{index}.json',
                   {'file': str(source.relative_to(exp)), 'input_sha256': fingerprint})

    print(f'{count} generated sources and validation records')
    for label in ('first', 'unchanged'):
        start = perf_counter()
        progress = description(exp, 'screen', config)
        elapsed = perf_counter() - start
        assert f'; {count}/{count} files;' in progress.prefix, progress
        print(f'{label} screen description: {elapsed:.6f} s', flush=True)
    with open(os.devnull, 'w') as sink, redirect_stdout(sink):
        start = perf_counter()
        for index in range(1000):
            log(exp, f'screen: recorded item {index}', config, display=UTC_DISPLAY)
        elapsed = perf_counter() - start
    assert len((exp/'state/log.txt').read_text().splitlines()) == 1000
    print(f'1,000 screen log calls: {elapsed:.6f} s')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=int, default=100)
    args = parser.parse_args()
    if args.sources < 1:
        parser.error('--sources must be positive')
    with TemporaryDirectory() as directory:
        benchmark(Path(directory), args.sources)
