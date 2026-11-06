#!/usr/bin/env python3
"""Remeasure anchor control after runner-only changes, preserving baseline evidence."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def recheck(root, tool):
    from test_survival.io import read_json, write_json
    result = {}
    for mode in ('all', 'affected'):
        original = root/'repo/experiments'/mode
        exp = root/f'control-recheck-{mode}'
        shutil.copytree(original, exp, ignore=shutil.ignore_patterns('work', 'state'))
        (exp/'state').mkdir()
        for stage in ('prepare', 'scope', 'generate', 'screen'):
            source = original/f'state/{stage}.json'
            if source.exists():
                shutil.copyfile(source, exp/f'state/{stage}.json')
        reference = root/f'reference-{mode}'
        for name in ('checkouts', 'libs'):
            shutil.copytree(reference/'work'/name, exp/'work'/name)
        for name in ('baseline-batches', 'control', 'raw', 'raw-derived', 'raw-coverage', 'coverage', 'selection-context'):
            shutil.rmtree(exp/'results'/name, ignore_errors=True)
        env = {**os.environ, 'PYTHONPATH': str(tool.resolve()/'src')}
        env.pop('SURVIVAL_JVM_JACOCO', None)
        command = [sys.executable, '-m', 'test_survival.cli', 'run', str(exp), '--stage', 'control', '--no-panel']
        started = time.monotonic()
        with (root/f'control-recheck-{mode}.log').open('w') as log:
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        seconds = time.monotonic()-started
        old = read_json(original/'results/baseline-batches/0.json')
        new = read_json(exp/'results/baseline-batches/0.json')
        fields = ('id', 'class', 'method', 'state', 'lines', 'dependency_record', 'last_executed_index', 'last_executed_sha')
        observations = lambda batch: sorted([{k: row[k] for k in fields if k in row} for row in batch['methods']], key=lambda r: r['id'])
        assert observations(old) == observations(new), 'Control observations changed'
        assert old['universe'] == new['universe'], 'Scope universe changed'
        compile_seconds = sum({a['log']: a['seconds'] for c in new['compile'] for a in c.get('attempts', [c])}.values())
        jvm = sum(r['seconds'] for r in new['runs'])
        ref = read_json(root/f'reference-{mode}.json')['seconds']
        result[mode] = dict(seconds=seconds, test_compile_seconds=compile_seconds, test_jvm_seconds=jvm,
                            other_seconds=seconds-compile_seconds-jvm, reference_seconds=ref, ratio=seconds/ref,
                            methods=len(new['methods']), observations_identical=True, universe_identical=True,
                            tool_head=subprocess.check_output(['git', '-C', str(tool), 'rev-parse', 'HEAD'], text=True).strip(),
                            command=command, log=str(root/f'control-recheck-{mode}.log'))
        write_json(root/'control-recheck.json', result)
        print(mode, json.dumps(result[mode]), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--tool', type=Path, required=True)
    args = parser.parse_args()
    recheck(args.root.resolve(), args.tool.resolve())
