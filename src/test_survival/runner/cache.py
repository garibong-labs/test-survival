"""Serialize Maven classpath resolution per pom content hash."""
import hashlib
from pathlib import Path
import shutil
import threading
import subprocess
import re
from ..config import canonical_hash
from ..adapters.maven_dependencies import resolution_timeout
from ..io import read_json, write_json

_LOCK = threading.Lock()


def rebase_classpath(text, root, index):
    """Reactor output paths move with the checkout; dependency identities do not."""
    return re.sub(re.escape(root)+r'/\d+(?=/)', lambda _:root+'/'+str(index), text)


class ClasspathCache:
    def __init__(self, exp, config, runner):
        self.exp, self.config, self.runner = Path(exp), config, runner

    def context_identity(self, index):
        directory = self.exp/f'work/context-inputs/{index}'
        directory.mkdir(parents=True, exist_ok=True)
        output = directory/'inputs.json'
        output.unlink(missing_ok=True)
        root = self.exp/f'work/checkouts/{index}'
        folder = root/self.config['project']['module']
        result = self.runner.execute(['python3', str(Path(__file__).parents[1]/'adapters/context_inputs.py'), str(root), str(folder), str(output)],
                                     folder, directory/'inputs.log', self.config['runner']['timeouts']['build'])
        if result['exit_code'] or not output.exists():
            raise RuntimeError('Cannot read selection context inputs')
        docker = self.config['runner']['docker']
        if self.runner.container_id:
            image = subprocess.check_output(['docker', 'inspect', '--format', '{{.Image}}', self.runner.container_id], text=True).strip()
        else:
            name = docker['image']
            if docker['compose_files']:
                command = ['docker','compose']
                for path in docker['compose_files']: command += ['-f',path]
                # Compose resolves its generated project-service image name for
                # build-only services; the optional JSON `image` field does not.
                name = subprocess.check_output([*command, 'config', '--images', docker['service']], text=True).strip()
                if not name or '\n' in name:
                    raise RuntimeError('Cannot identify the Compose runner image')
            image = subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',name], text=True).strip()
        manifest = Path(__file__).parents[1]/'adapters/java/classpath-artifacts.json'
        return canonical_hash(dict(version=3, inputs=read_json(output), image=image, module=self.config['project']['module'],
                                   runner_libraries=hashlib.sha256(manifest.read_bytes()).hexdigest()))

    def resolve(self, folder, index, *, log_root=None):
        digest = hashlib.sha256((folder / 'pom.xml').read_bytes()).hexdigest()
        affected = self.config['survival'].get('selection') == 'affected'
        enabled = self.config['runner']['cache']['classpath'] == 'pom-sha256'
        identity = self.context_identity(index) if affected else digest
        cache = self.exp / 'work/classpaths' / f'{identity if enabled else index}.txt'
        attempts = []
        with _LOCK:
            cache.parent.mkdir(parents=True, exist_ok=True)
            reuse = self.config['runner']['reuse']['classpath_cache']
            if enabled and not affected and reuse and not cache.exists() and (Path(reuse) / cache.name).is_file():
                shutil.copy2(Path(reuse) / cache.name, cache)
            if not (affected and enabled and cache.is_file()):
                # Maven paths belong to the container, not the orchestrator's filesystem.
                job = self.exp / f'work/classpath-jobs/{index}.json'
                output = f'work/classpath-jobs/{index}-result.json'
                logs = log_root or self.exp/'results/builds/logs'
                write_json(job, dict(action='classpath', logs=str(logs.relative_to(self.exp)), experiment=self.runner.path(self.exp),
                                    folder=self.runner.path(folder), module=self.config['project']['module'],
                                    cache=str(cache.relative_to(self.exp)), index=index, force=not enabled,
                                    jvm={}, timeouts=self.config['runner']['timeouts'], output=output))
                timeout = resolution_timeout(self.config['runner']['timeouts']['build'])+600
                result = self.runner.execute(['python3', str(Path(__file__).parents[1]/'adapters/java_worker.py'), str(job)],
                                             folder, logs/f'classpath-{index}.txt', timeout)
                if result['exit_code'] or not cache.exists():
                    raise RuntimeError('Maven classpath resolution failed; see '+result['log'])
                attempts = read_json(self.exp/output)['attempts']
            # Retain the resolved template and relocate only this experiment's
            # reactor output paths, without invoking Maven again for the next index.
            root = self.runner.path(self.exp/'work/checkouts') if affected else ''
            if affected and root in cache.read_text():
                local = cache.with_name(f'{identity}-{index}.txt')
                local.write_text(rebase_classpath(cache.read_text(), root, index))
                cache = local
        return digest, cache, attempts
