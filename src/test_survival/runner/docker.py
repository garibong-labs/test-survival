"""Container command execution with explicit path translation and evidence."""
from contextlib import contextmanager
import os
import re
from pathlib import Path
import signal
import subprocess
import time

from ..io import atomic_text, write_json, now
from ..lifecycle import container_options, launch


class DockerRunner:
    def __init__(self, experiment, config):
        self.exp = Path(experiment).resolve()
        self.container_id = None
        self.config = config
        docker = config['runner']['docker']
        self.mounts = {str(Path(h).resolve()): c.rstrip('/') for h, c in docker['mounts'].items()}
        self.package = Path(__file__).resolve().parents[1]
        self.mounts[str(self.package)] = '/survival-package'
        self.mounts.setdefault(str(self.exp), '/experiment') if not any(self.exp.is_relative_to(h) for h in self.mounts) else None
        self.read_only = {str(self.package)}
        for source in config['runner'].get('policy_read_only', []):
            host = str(Path(source).resolve())
            self.mounts[host] = self.path(host)
            self.read_only.add(host)

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
        if self.container_id:
            return ['docker', 'exec', '--workdir', self.path(cwd), self.container_id, *[self.argument(v) for v in command]]
        d = self.config['runner']['docker']
        if d['compose_files']:
            prefix = ['docker', 'compose']
            for file in d['compose_files']:
                prefix += ['-f', file]
            prefix += ['run', '--rm', '--no-deps', '-T', '--workdir', self.path(cwd)]
            for host, container in self.mounts.items():
                prefix += ['--volume', f'{host}:{container}' + (':ro' if host in self.read_only else '')]
            prefix += [d['service']]
        elif d['image']:
            prefix = ['docker', 'run', '--rm', '--workdir', self.path(cwd)]
            if not any('/root/.m2' == target or '/root/.m2'.startswith(target+'/') for target in self.mounts.values()):
                prefix += ['--volume', d['m2_volume']+':/root/.m2']
            for host, container in self.mounts.items():
                prefix += ['--volume', f'{host}:{container}' + (':ro' if host in self.read_only else '')]
            if d['env_file']:
                prefix += ['--env-file', d['env_file']]
            prefix += [d['image']]
        else:
            raise RuntimeError('runner.docker requires compose_files or image')
        prefix[prefix.index('run')+1:prefix.index('run')+1] = container_options(self.exp)
        return prefix + [self.argument(v) for v in command]

    def execute(self, command, cwd, log, timeout=600):
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        timed_out = False
        with log.open('w') as stream:
            child = launch(self.exp, self.command(command, cwd), stdout=stream, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                code = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(child.pid, signal.SIGKILL)
                code = child.wait()
        return dict(command=[self.argument(c) for c in command], cwd=self.path(cwd), exit_code=code,
                    timeout=timed_out, seconds=round(time.monotonic()-started, 3), log=str(log.relative_to(self.exp)))

    @contextmanager
    def session(self, stage):
        """Share the original runner's network and memory boundary across workers."""
        command = self.command(['sleep', 'infinity'], self.exp)
        command.insert(command.index('run') + 1, '--detach')
        with launch(self.exp, command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
            output, error = child.communicate()
        if child.returncode:
            path = self.exp / f'state/container-{stage}.log'
            atomic_text(path, error)
            raise RuntimeError('KTester container could not be started; see container stage log')
        ident = output.strip().splitlines()[-1]
        if not re.fullmatch(r'[0-9a-f]{12,64}', ident):
            raise RuntimeError('Docker did not return a container identifier')
        self.container_id = ident
        record = dict(container=ident, stage=stage, started=now(), status='running')
        write_json(self.exp / f'state/container-{stage}.json', record)
        try:
            yield self
        finally:
            subprocess.run(['docker', 'stop', '--time', '5', ident], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.container_id = None
            record.update(status='stopped', finished=now())
            write_json(self.exp / f'state/container-{stage}.json', record)
