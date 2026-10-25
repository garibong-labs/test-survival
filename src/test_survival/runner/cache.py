"""Serialize Maven classpath resolution per pom content hash."""
import hashlib
from pathlib import Path
import shutil
import threading
from ..io import read_json, write_json

_LOCK = threading.Lock()


class ClasspathCache:
    def __init__(self, exp, config, runner):
        self.exp, self.config, self.runner = Path(exp), config, runner

    def resolve(self, folder, index):
        digest = hashlib.sha256((folder / 'pom.xml').read_bytes()).hexdigest()
        enabled = self.config['runner']['cache']['classpath'] == 'pom-sha256'
        cache = self.exp / 'work/classpaths' / f'{digest if enabled else index}.txt'
        attempts = []
        with _LOCK:
            cache.parent.mkdir(parents=True, exist_ok=True)
            reuse = self.config['runner']['reuse']['classpath_cache']
            if enabled and reuse and not cache.exists() and (Path(reuse) / cache.name).is_file():
                shutil.copy2(Path(reuse) / cache.name, cache)
            # Maven paths belong to the container, not the orchestrator's filesystem.
            from ..adapters.maven_dependencies import RETRIES, BACKOFF_SECONDS
            job = self.exp / f'work/classpath-jobs/{index}.json'
            output = f'work/classpath-jobs/{index}-result.json'
            write_json(job, dict(action='classpath', experiment=self.runner.path(self.exp),
                                folder=self.runner.path(folder), module=self.config['project']['module'],
                                cache=str(cache.relative_to(self.exp)), index=index, force=not enabled,
                                jvm={}, timeouts=self.config['runner']['timeouts'], output=output))
            timeout = 2*((RETRIES+2)*self.config['runner']['timeouts']['build']+RETRIES*BACKOFF_SECONDS)+600
            result = self.runner.execute(['python3', str(Path(__file__).parents[1]/'adapters/java_worker.py'), str(job)],
                                         folder, self.exp/f'results/builds/logs/classpath-{index}.txt', timeout)
            if result['exit_code'] or not cache.exists():
                raise RuntimeError('Maven classpath resolution failed; see '+result['log'])
            attempts = read_json(self.exp/output)['attempts']
        return digest, cache, attempts
