"""Container command execution with explicit path translation and evidence."""
import os
import re
from pathlib import Path
import signal
import subprocess
import time

from ..io import atomic_text


class DockerRunner:
    def __init__(self, experiment, config):
        self.exp = Path(experiment).resolve()
        self.config = config
        docker = config['runner']['docker']
        self.mounts = {str(Path(h).resolve()): c.rstrip('/') for h, c in docker['mounts'].items()}
        self.package = Path(__file__).resolve().parents[1]
        self.mounts[str(self.package)] = '/survival-package'
        self.mounts.setdefault(str(self.exp), '/experiment') if not any(self.exp.is_relative_to(h) for h in self.mounts) else None

    def path(self, value):
        value = str(value)
        for host, container in sorted(self.mounts.items(), key=lambda pair: -len(pair[0])):
            if value == host or value.startswith(host + '/'):
                return container + value[len(host):]
        return value

    def argument(self, value):
        value = str(value)
        for host, container in sorted(self.mounts.items(), key=lambda pair: -len(pair[0])):
            value = re.sub(r'(^|[=:])'+re.escape(host)+r'(?=/|:|,|$)',lambda m:m.group(1)+container,value)
        return value

    def command(self, command, cwd):
        d = self.config['runner']['docker']
        if d['compose_files']:
            prefix = ['docker', 'compose']
            for file in d['compose_files']:
                prefix += ['-f', file]
            prefix += ['run', '--rm', '--no-deps', '-T', '--workdir', self.path(cwd)]
            for host, container in self.mounts.items():
                prefix += ['--volume', f'{host}:{container}' + (':ro' if host == str(self.package) else '')]
            prefix += [d['service']]
        elif d['image']:
            prefix = ['docker', 'run', '--rm', '--workdir', self.path(cwd)]
            for host, container in self.mounts.items():
                prefix += ['--volume', f'{host}:{container}' + (':ro' if host == str(self.package) else '')]
            if d['env_file']:
                prefix += ['--env-file', d['env_file']]
            prefix += [d['image']]
        else:
            raise RuntimeError('runner.docker requires compose_files or image')
        return prefix + [self.argument(v) for v in command]

    def execute(self, command, cwd, log, timeout=600):
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        timed_out = False
        with log.open('w') as stream:
            child = subprocess.Popen(self.command(command, cwd), stdout=stream, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                code = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(child.pid, signal.SIGKILL)
                code = child.wait()
        return dict(command=[self.argument(c) for c in command], cwd=self.path(cwd), exit_code=code,
                    timeout=timed_out, seconds=round(time.monotonic()-started, 3), log=str(log.relative_to(self.exp)))
