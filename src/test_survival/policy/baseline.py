"""Read-only baseline access, including pre-framework evidence."""
from pathlib import Path
import subprocess

from ..config import ConfigError, as_written, load, validate
from ..io import read_csv, read_json
from .config import merge


class Baseline:
    def __init__(self, exp, config):
        self.exp = Path(exp).resolve()
        self.options = config.get('policies')
        if not self.options:
            raise ConfigError('policies: configuration required')
        repo = Path(config['project']['repo']).expanduser().resolve()
        self.path = self.resolve(self.options['baseline'], repo)
        self.guard(self.exp)
        # Reject symlinked output trees, including links buried in resumed jobs.
        if self.exp.exists():
            for path in self.exp.rglob('*'):
                if path.is_symlink():
                    self.guard(path)
        source = self.path/'experiment.yaml'
        if not source.exists():
            fallback = self.options['baseline_config']
            if not fallback:
                raise ConfigError('Pre-framework baseline requires policies.baseline_config')
            source = self.resolve(fallback, repo)
        self.config = load(source)
        self.repo = Path(self.config['project']['repo']).expanduser().resolve()
        self.groups = self.options['groups']
        self.frozen = read_json(self.path/'results/frozen-denominators.json')
        if self.frozen is None or set(self.groups) - self.frozen.keys():
            raise ConfigError('policies.groups must be frozen-denominators.json keys')
        self.commits = read_csv(self.path/'results/commits.csv')
        self.curves = read_csv(self.path/'results/survival.csv')
        self.anchor = read_json(self.path/'results/anchor.json')
        self.scope = read_json(self.path/'population/scope.json')
        expected = [0, *[int(r['index']) for r in self.commits]]
        if [int(r['index']) for r in self.curves] != expected:
            raise ConfigError('Baseline survival.csv is incomplete or out of order')
        if not (self.path/'results/changed-lines.csv').exists():
            raise ConfigError('Baseline must have completed coverage and reporting')
        for g in self.groups:
            if int(self.curves[0][g+'_alive']) != len(self.frozen[g]):
                raise ConfigError('Baseline index zero differs from frozen denominator: '+g)
        self.dev = {g['name']: g for g in self.config['dev']['groups']}
        configured = self.dev.keys() | {g['name'] for g in self.config['generators']}
        if set(self.groups) - configured:
            raise ConfigError('baseline_config lacks settings for selected groups')
        if set(self.options['generator_overrides']) - {g['name'] for g in self.config['generators']}:
            raise ConfigError('generator_overrides contains a non-generator group')
        settings = as_written(self.config, include_defaults=True)
        settings.pop('policies', None)
        settings['dev'].pop('denominator', None)
        settings['generators'] = [merge(g, self.options['generator_overrides'].get(g['name'], {})) for g in settings['generators']]
        self.execution_config = validate(settings)
        # Internal execution constraint, propagated through generator config copies.
        self.execution_config['runner']['policy_read_only'] = [str(self.path)]
        skip_file = self.path/'results/skipped-commits.csv'
        if skip_file.exists():
            self.skipped = {int(r['index']): r['reason'] for r in read_csv(skip_file)}
        else:
            # Missing metadata is only safe when the complete raw grid is present.
            missing = [(r['index'], g) for r in self.commits for g in self.frozen if not self.raw_path(int(r['index']), g).exists()]
            if missing:
                raise ConfigError(f'Baseline lacks skip metadata and raw records: {missing[:5]}')
            self.skipped = {}
        self.by_index = {int(r['index']): r for r in self.curves}

    @staticmethod
    def resolve(value, repo):
        path = Path(value).expanduser()
        return (path if path.is_absolute() else repo/path).resolve()

    def guard(self, path):
        if Path(path).resolve().is_relative_to(self.path) or self.path.is_relative_to(Path(path).resolve()):
            raise ConfigError('Policy writes cannot land inside the read-only baseline: '+str(path))
        return Path(path)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    def raw_path(self, index, group):
        for folder in ('raw-derived', 'raw'):
            path = self.path/f'results/{folder}/{index}-{group}.json'
            if path.exists():
                return path
        return self.path/f'results/raw/{index}-{group}.json'

    def raw(self, index, group):
        result = read_json(self.raw_path(index, group))
        if result is None:
            raise ConfigError(f'Missing baseline raw record: {index}/{group}')
        return result

    def evaluable(self, index):
        if index in self.skipped or self.by_index[index]['evaluated'] != 'yes':
            return False
        return read_json(self.path/f'results/builds/{index}.json', {}).get('build', 'ok') == 'ok'

    def commit_paths(self):
        saved = read_json(self.path/'results/commit-paths.json')
        if saved is not None:
            return saved
        return {r['index']: self.git('diff', '--no-renames', '--name-only', r['sha']+'^1', r['sha']).splitlines() for r in self.commits}
