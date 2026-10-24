"""Record report completion in each experiment-owned KTester dataset."""
import hashlib
import subprocess

from ..io import read_json, write_json


def record(exp, config, summary):
    for group in config['generators']:
        if group['method'] != 'ktester':
            continue
        dataset = exp/'work/ktester'/group['name']
        if not (dataset/'dataset_info.json').exists():
            continue  # Imported generation does not create an author-tool dataset.
        def git(*args):
            return subprocess.check_output(['git', '-C', config['project']['repo'], *args], text=True).strip()
        result = dict(
            status=summary['status'], fork_commit=git('rev-parse', 'HEAD'),
            fork_branch=git('branch', '--show-current'),
            anchor=read_json(exp/'results/anchor.json'), denominators=summary['denominators'],
            model_rounds=summary['model_rounds'], phase_seconds=summary['phase_seconds'],
            verification=summary['final_verification'],
            final_summary_sha256=hashlib.sha256((exp/'results/final-summary.json').read_bytes()).hexdigest(),
            report=str(exp/'README.md'), author_code_modified=False, pushed=False)
        write_json(dataset/'experiment-result.json', result)
