"""Successful javac artifacts, isolated from test execution and result caching.

Only the worker's explicit classpath/sourcepath command form is supported. Failed
compilations are always executed again; their diagnostics remain observations.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from uuid import uuid4
import zipfile


class Uncacheable(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def save(path, value):
    """Publish a complete JSON file, including when a worker is interrupted."""
    temporary = path.with_name(path.name+'.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class CompilationService:
    def __init__(self, experiment, execute, context, backend='subprocess'):
        self.exp, self.execute, self.context = Path(experiment), execute, context
        self.root = self.exp/'work/compile-cache/v1'
        self.hashes = {}
        self.toolchain = None
        self.backend = backend

    def file_hash(self, path):
        if path.is_symlink():
            raise Uncacheable('symlink input')
        stat = path.stat()
        stamp = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        previous = self.hashes.get(path)
        if previous and previous[0] == stamp:
            return previous[1]
        checksum = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024*1024), b''):
                checksum.update(block)
        value = checksum.hexdigest()
        self.hashes[path] = (stamp, value)
        return value

    def snapshot(self, path):
        if path.is_symlink():
            raise Uncacheable('symlink input')
        if path.is_file():
            return self.file_hash(path)
        if not path.is_dir():
            raise Uncacheable('missing input')
        files = {}
        for child in sorted(path.rglob('*')):
            if child.is_symlink():
                raise Uncacheable('symlink input')
            if child.is_file():
                # javac's default source/class preference also observes modification times.
                files[str(child.relative_to(path))] = [self.file_hash(child), child.stat().st_mtime_ns]
        return files

    def compiler_identity(self, cwd, event, timeout):
        executable = shutil.which('javac')
        if not executable:
            raise Uncacheable('javac not found')
        executable = Path(executable).resolve()
        if self.toolchain is None or self.toolchain[1][0] != executable:
            probe = self.execute(['javac', '-J-XshowSettings:properties', '-version'], cwd,
                                 event/'toolchain.log', min(timeout, 30))
            save(event/'toolchain.json', probe)
            text = (event/'toolchain.log').read_text(errors='replace')
            home = re.search(r'^\s*java.home = (.+)$', text, re.M)
            if probe['exit_code'] or probe.get('timeout') or not home:
                raise Uncacheable('unverified JDK')
            home = Path(home[1].strip()).resolve()
            paths = [executable, home/'release', home/'lib/modules', home/'lib/ct.sym']
            # Contents, not an image tag or version string alone, identify javac.
            paths += [p for p in (home/'lib/server/libjvm.so', home/'lib/server/libjvm.dylib') if p.exists()]
            self.toolchain = (text, paths)
        version, paths = self.toolchain
        return {'version': version, 'files': [(str(p), self.file_hash(p)) for p in paths]}

    def inputs(self, sources, sourcepath, classpath, cwd, heap, timeout, event):
        if any(os.environ.get(name) for name in (
                'JAVA_TOOL_OPTIONS', '_JAVA_OPTIONS', 'JDK_JAVA_OPTIONS', 'JDK_JAVAC_OPTIONS',
                'LD_LIBRARY_PATH', 'LD_PRELOAD', 'DYLD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES')):
            raise Uncacheable('external compiler options')
        entries = classpath.split(os.pathsep)
        if not all(entries) or any('*' in p for p in entries):
            raise Uncacheable('implicit or wildcard classpath')
        services = ('META-INF/services/javax.annotation.processing.Processor',
                    'META-INF/services/com.sun.source.util.Plugin')
        for entry in entries:
            path = Path(entry)
            if not path.is_absolute():
                raise Uncacheable('relative classpath')
            if path.is_dir():
                if any((path/name).exists() for name in services):
                    raise Uncacheable('compiler extension')
            elif path.is_file():
                with zipfile.ZipFile(path) as jar:
                    if any(name in jar.namelist() for name in services):
                        raise Uncacheable('compiler extension')
                    if 'META-INF/MANIFEST.MF' in jar.namelist():
                        manifest = jar.read('META-INF/MANIFEST.MF').decode(errors='replace')
                        if re.search(r'^Class-Path:', manifest, re.M | re.I):
                            raise Uncacheable('manifest classpath')
        environment = {name: os.environ.get(name) for name in (
            'JAVA_HOME', 'JAVA_TOOL_OPTIONS', '_JAVA_OPTIONS', 'JDK_JAVA_OPTIONS',
            'JDK_JAVAC_OPTIONS', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'SOURCE_DATE_EPOCH')}
        return {
            'schema': 1, 'commit': self.context['sha'], 'cwd': str(cwd), 'backend': self.backend,
            'implementation_sha256': self.file_hash(Path(__file__).resolve()),
            'compiler': self.compiler_identity(cwd, event, timeout),
            'environment_sha256': digest(environment), 'heap': heap, 'timeout': timeout,
            'sources': [(str(p), self.file_hash(p)) for p in sources],
            'sourcepath': (str(sourcepath), self.snapshot(sourcepath)),
            'classpath': [(p, self.snapshot(Path(p))) for p in entries],
        }

    @contextmanager
    def locked(self, key):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root/(key+'.lock')).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def cached(self, entry, key):
        try:
            if entry.is_symlink():
                return None
            record = json.loads((entry/'manifest.json').read_text())
            if (record['key'] == key and record['schema'] == 1
                    and re.fullmatch('[0-9a-f]{32}', record['origin_event'])
                    and record['outputs'] == self.snapshot(entry/'classes')
                    and record['log_sha256'] == self.file_hash(entry/'javac.txt')):
                return record
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def publish(self, entry, key, classes, log, event_id):
        temporary = Path(tempfile.mkdtemp(prefix=key+'.', dir=self.root))
        try:
            shutil.copytree(classes, temporary/'classes')
            shutil.copy2(log, temporary/'javac.txt')
            save(temporary/'manifest.json', dict(schema=1, key=key, origin_event=event_id,
                 outputs=self.snapshot(temporary/'classes'), log_sha256=self.file_hash(temporary/'javac.txt')))
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            elif entry.exists():
                shutil.rmtree(entry)
            temporary.replace(entry)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def compile(self, sources, sourcepath, classpath, classes, cwd, log, heap, timeout):
        sources = list(sources)
        command = ['javac', '-J-Xmx'+heap, '-cp', classpath, '-sourcepath', str(sourcepath),
                   '-d', str(classes), *map(str, sources)]
        event_id = uuid4().hex
        event = self.exp/'results/compile-events'/event_id
        event.mkdir(parents=True)
        started = time.monotonic()
        save(event/'started.json', dict(context=self.context, command=command,
             started=datetime.now(timezone.utc).isoformat()))
        cache = {'status': 'bypass', 'event': str(event.relative_to(self.exp))}
        try:
            if classes.exists():
                shutil.rmtree(classes)
            classes.mkdir(parents=True)
            try:
                key = digest(self.inputs(sources, sourcepath, classpath, cwd, heap, timeout, event))
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                cache['reason'] = str(exc) if isinstance(exc, Uncacheable) else type(exc).__name__
                result = self.execute(command, cwd, log, timeout)
            else:
                cache.update(status='miss', key=key)
                with self.locked(key):
                    entry = self.root/key
                    # Inputs may have changed while another process held this key's lock.
                    try:
                        current = digest(self.inputs(sources, sourcepath, classpath, cwd, heap, timeout, event))
                    except (OSError, ValueError, zipfile.BadZipFile):
                        current = None
                    if current != key:
                        cache.update(status='bypass', reason='inputs changed while waiting for cache')
                    previous = self.cached(entry, key) if current == key else None
                    if previous:
                        # Test code may modify its output tree. Never expose cache files to it.
                        if classes.exists():
                            shutil.rmtree(classes)
                        shutil.copytree(entry/'classes', classes)
                        log.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(entry/'javac.txt', log)
                        result = dict(command=command, cwd=str(cwd), exit_code=0, timeout=False,
                                      seconds=0, log=str(log.relative_to(self.exp)))
                        cache.update(status='hit', origin_event=previous['origin_event'])
                        try:
                            restored = digest(self.inputs(sources, sourcepath, classpath, cwd, heap, timeout, event))
                        except (OSError, ValueError, zipfile.BadZipFile):
                            restored = None
                        if restored != key:
                            cache.update(status='bypass', reason='inputs changed during restore')
                            cache.pop('origin_event')
                            shutil.rmtree(classes)
                            classes.mkdir()
                            result = self.execute(command, cwd, log, timeout)
                    else:
                        result = self.execute(command, cwd, log, timeout)
                        if current == key and result['exit_code'] == 0 and not result.get('timeout'):
                            # Do not publish if the compile's inputs changed while it ran.
                            try:
                                after = digest(self.inputs(sources, sourcepath, classpath, cwd, heap, timeout, event))
                            except (OSError, ValueError, zipfile.BadZipFile):
                                after = None
                            if after == key:
                                self.publish(entry, key, classes, log, event_id)
                            else:
                                cache['reason'] = 'inputs changed during compilation'
            cache['elapsed_seconds'] = round(time.monotonic()-started, 3)
            result['cache'] = cache
            shutil.copyfile(log, event/'javac.txt')
            save(event/'finished.json', dict(finished=datetime.now(timezone.utc).isoformat(),
                 executed=cache['status'] != 'hit', result={**result, 'log': str((event/'javac.txt').relative_to(self.exp))}))
            return result
        except BaseException as exc:
            save(event/'interrupted.json', {'error_type': type(exc).__name__})
            raise
