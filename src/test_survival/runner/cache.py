"""Serialize Maven classpath resolution per pom content hash."""
import hashlib
from pathlib import Path
import shutil
import threading

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
            if not enabled or not cache.exists():
                for attempt, offline in enumerate((True, False), 1):
                    result = self.runner.execute(['mvn', '-q', *(['-o'] if offline else []), 'dependency:build-classpath',
                                                  '-DincludeScope=test', '-Dmdep.outputFile='+str(cache)], folder,
                                                 self.exp / f'results/builds/logs/classpath-{index}-{attempt}.txt',
                                                 self.config['runner']['timeouts']['build'])
                    attempts.append(result)
                    if result['exit_code'] == 0 and cache.exists():
                        break
            if not cache.exists():
                raise RuntimeError('Maven classpath resolution failed; see build logs')
        return digest, cache, attempts
