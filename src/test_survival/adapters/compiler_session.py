"""Opt-in, group-scoped compiler JVM. Unknown inputs always use the original CLI."""
import hashlib
import os
from pathlib import Path
import re
import select
import shutil
import signal
import struct
import subprocess
import threading
import time
import zipfile


MAGIC = 0x43534A31
MAX_REQUESTS = 64
EXTERNAL_OPTIONS = ('JAVA_TOOL_OPTIONS', '_JAVA_OPTIONS', 'JDK_JAVA_OPTIONS', 'JDK_JAVAC_OPTIONS',
                    'LD_LIBRARY_PATH', 'LD_PRELOAD', 'DYLD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES')
PLUGIN_SERVICE = 'META-INF/services/com.sun.source.util.Plugin'


class UnsupportedJdk(OSError):
    """The server did not qualify the JDK required by the restricted command shape."""


def file_stamp(path):
    try:
        stat = path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except FileNotFoundError:
        return None


class CompilerSession:
    def __init__(self, experiment, execute, mode='subprocess'):
        self.exp, self.fallback, self.mode = Path(experiment), execute, mode
        self.helper = Path(__file__).with_name('java')/'CompilerServer.java'
        self.child = self.stderr = self.signature = None
        self.unsupported_signature = None
        self.requests = self.sequence = 0
        self.lock = threading.Lock()

    @property
    def identity(self):
        if self.mode == 'subprocess':
            return 'subprocess'
        return {'mode': self.mode, 'python': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'java': hashlib.sha256(self.helper.read_bytes()).hexdigest()}

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def close(self):
        if self.child is not None:
            if self.child.poll() is None:
                try:
                    os.killpg(self.child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.child.wait()
            self.child.stdin.close()
            self.child.stdout.close()
            self.child = None
        if self.stderr is not None:
            self.stderr.close()
            self.stderr = None
        self.signature = None
        self.requests = 0

    def supported(self, command):
        if self.mode != 'reused-jvm':
            return 'disabled'
        if any(os.environ.get(k) for k in EXTERNAL_OPTIONS):
            return 'external compiler/JVM options'
        if (len(command) < 9 or command[0] != 'javac' or
                not re.fullmatch(r'-J-Xmx[1-9][0-9]*[kKmMgG]', command[1]) or
                command[2:3] != ['-cp'] or command[4:5] != ['-sourcepath'] or command[6:7] != ['-d']):
            return 'unsupported command shape'
        if any('@' == arg[:1] or '*' in arg for arg in command):
            return 'argfile or wildcard'
        paths = [Path(command[5]), *map(Path, command[8:])]
        if any(not p.is_absolute() or p.is_symlink() or not p.exists() for p in paths):
            return 'unsupported source path'
        if not paths[0].is_dir():
            return 'unsupported source directory'
        if any(p.suffix != '.java' or not p.is_file() for p in paths[1:]):
            return 'unsupported source file'
        output = Path(command[7])
        if (not output.is_absolute() or output.is_symlink() or
                not output.resolve().is_relative_to((self.exp/'work').resolve()) or
                output.resolve() == (self.exp/'work').resolve() or
                any(p.resolve().is_relative_to(output.resolve()) for p in paths) or
                output.resolve().is_relative_to(paths[0].resolve())):
            return 'output is not isolated worker scratch'
        for entry in command[3].split(os.pathsep):
            path = Path(entry)
            if not entry or not path.is_absolute() or path.is_symlink():
                return 'implicit/relative/symlink classpath'
            if (output.resolve().is_relative_to(path.resolve()) or
                    path.resolve().is_relative_to(output.resolve())):
                return 'output overlaps classpath'
            if path.is_dir():
                if (path/PLUGIN_SERVICE).exists():
                    return 'compiler plugin'
            elif path.is_file():
                try:
                    with zipfile.ZipFile(path) as jar:
                        if PLUGIN_SERVICE in jar.namelist():
                            return 'compiler plugin'
                        # Processor registration alone is inactive on qualified JDK 25.
                        # The accepted shape has no processing options and all external
                        # JVM/javac options are rejected above. Plugins can autoStart.
                        if 'META-INF/MANIFEST.MF' in jar.namelist():
                            text = jar.read('META-INF/MANIFEST.MF').decode(errors='replace')
                            if re.search(r'^Class-Path:', text, re.M | re.I):
                                return 'manifest classpath'
                except (OSError, zipfile.BadZipFile):
                    return 'unreadable classpath'
            else:
                return 'missing classpath'
        return None

    def _read(self, count, deadline):
        data = b''
        while len(data) < count:
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not select.select([self.child.stdout], [], [], remaining)[0]:
                raise TimeoutError('compiler request timed out')
            part = os.read(self.child.stdout.fileno(), count-len(data))
            if not part:
                raise OSError('compiler worker exited')
            data += part
        return data

    def _write(self, data, deadline):
        fd = self.child.stdin.fileno()
        while data:
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                raise TimeoutError('compiler request timed out')
            try:
                n = os.write(fd, data)
                data = data[n:]
            except BlockingIOError:
                continue

    def _start(self, java, heap, cwd, log, deadline):
        self.stderr = log.with_name(log.name+'.jvm-stderr').open('wb')
        # Source-launching the helper must not search ambient CLASSPATH/cwd for plugins.
        self.child = subprocess.Popen([str(java), '-Xmx'+heap, '-cp', str(self.helper.parent), str(self.helper)], cwd=cwd,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                                      start_new_session=True, bufsize=0)
        os.set_blocking(self.child.stdin.fileno(), False)
        magic, version = struct.unpack('>ii', self._read(8, deadline))
        if magic != MAGIC:
            raise OSError('invalid compiler handshake')
        if version != 25:
            raise UnsupportedJdk('compiler JVM requires JDK 25, got '+str(version))

    def execute(self, command, cwd, log, timeout=600):
        with self.lock:  # No concurrent CompilationTask or protocol interleaving.
            return self._execute(list(command), Path(cwd), Path(log), timeout)

    def _execute(self, command, cwd, log, timeout):
        start = time.monotonic()
        try:
            reason = self.supported(command)
        except OSError:
            reason = 'unreadable compiler inputs'
        if reason:
            if self.mode == 'reused-jvm':
                self.close()
            remaining = timeout if self.mode == 'subprocess' else max(.001, timeout-(time.monotonic()-start))
            result = self.fallback(command, cwd, log, remaining)
            if self.mode != 'subprocess':
                result['seconds'] = round(time.monotonic()-start, 3)
                result['compiler_backend'] = {'mode': 'subprocess', 'reason': reason}
            return result
        deadline = start+timeout
        log.parent.mkdir(parents=True, exist_ok=True)
        output = Path(command[7])
        cold = False
        signature = None
        try:
            log.write_text('')  # A startup failure must not inherit an earlier request's log.
            javac = shutil.which('javac')
            if javac is None:
                raise OSError('javac not found')
            javac = Path(javac).resolve()
            java = javac.with_name('java')
            home = java.parent.parent
            jdk_files = [javac, java, home/'release', home/'lib/modules', home/'lib/ct.sym',
                         home/'lib/server/libjvm.so', home/'lib/server/libjvm.dylib']
            signature = (tuple((str(p), file_stamp(p)) for p in jdk_files), command[1], str(cwd.resolve()),
                         tuple(sorted(os.environ.items())))
            if signature == self.unsupported_signature:
                raise UnsupportedJdk('compiler JVM requires JDK 25 (cached handshake)')
            if signature != self.signature or self.requests >= MAX_REQUESTS or self.child.poll() is not None:
                self.close()
                cold = True
                self._start(java, command[1][6:], cwd, log, deadline)
                self.signature = signature
            self.sequence += 1
            values = [str(log.resolve()), command[3], command[5], command[7], *command[8:]]
            encoded = [v.encode('utf-8') for v in values]
            if len(values) > 8192 or any(len(v) > 1024*1024 for v in encoded):
                raise OSError('compiler request too large')
            packet = struct.pack('>ii', self.sequence, len(values))
            packet += b''.join(struct.pack('>i', len(v))+v for v in encoded)
            self._write(packet, deadline)
            magic, ident, code, fault = struct.unpack('>iiii', self._read(16, deadline))
            if magic != MAGIC or ident != self.sequence or code not in (0, 1) or fault:
                raise OSError('compiler protocol or task failure')
            self.requests += 1
            return dict(command=command, cwd=str(cwd), exit_code=code, timeout=False,
                        seconds=round(time.monotonic()-start, 3), log=str(log.relative_to(self.exp)),
                        compiler_backend={'mode': 'reused-jvm', 'pid': self.child.pid, 'cold': cold,
                                          'request': self.sequence})
        except (OSError, TimeoutError) as exc:
            timed_out = isinstance(exc, TimeoutError)
            discarded = self.child is not None
            if isinstance(exc, UnsupportedJdk):
                self.unsupported_signature = signature
            self.close()  # Never reuse a worker with an uncertain request outcome.
            if log.exists():
                shutil.copyfile(log, log.with_name(log.name+'.jvm-failure'))
            if output.exists():
                shutil.rmtree(output)
            output.mkdir(parents=True)
            remaining = deadline-time.monotonic()
            if timed_out or remaining <= 0:
                log.write_text('Compiler JVM request timed out; worker discarded\n')
                return dict(command=command, cwd=str(cwd), exit_code=-9, timeout=True,
                            seconds=round(time.monotonic()-start, 3), log=str(log.relative_to(self.exp)),
                            compiler_backend={'mode': 'reused-jvm', 'discarded': discarded, 'reason': str(exc)})
            result = self.fallback(command, cwd, log, remaining)
            result['seconds'] = round(time.monotonic()-start, 3)
            result['compiler_backend'] = {'mode': 'subprocess', 'discarded': discarded, 'reason': str(exc)}
            return result
        except BaseException:
            self.close()
            raise
