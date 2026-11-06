"""Container-side content identity; persist digests, never Maven credentials."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def collect(root):
    root = Path(root)
    files = {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob('pom.xml'))}
    for p in sorted((root/'.mvn').rglob('*')) if (root/'.mvn').exists() else []:
        if p.is_file(): files[str(p.relative_to(root))] = digest(p)
    executable = Path(shutil.which('mvn') or '/missing-mvn').resolve()
    jvm_config = root/'.mvn/jvm.config'
    jvm_args = ' '.join(os.environ.get(k, '') for k in ('JAVA_TOOL_OPTIONS','JDK_JAVA_OPTIONS','MAVEN_OPTS'))
    if jvm_config.is_file(): jvm_args += ' '+jvm_config.read_text()
    properties = dict(arg[2:].split('=',1) for arg in shlex.split(jvm_args) if arg.startswith('-D') and '=' in arg)
    user_home = Path(properties.get('user.home', str(Path.home())))
    global_home = Path(os.environ.get('MAVEN_HOME') or os.environ.get('M2_HOME') or executable.parent.parent)
    settings = {label: digest(p) for label,p in (('user', user_home/'.m2/settings.xml'),
        ('global', global_home/'conf/settings.xml'), ('user-toolchains', user_home/'.m2/toolchains.xml'),
        ('global-toolchains', global_home/'conf/toolchains.xml'), ('security', user_home/'.m2/settings-security.xml'))}
    config = root/'.mvn/maven.config'
    args = shlex.split(os.environ.get('MAVEN_ARGS', '')) + shlex.split(config.read_text() if config.is_file() else '')
    for i, arg in enumerate(args):
        if arg in ('-s', '--settings', '-gs', '--global-settings', '-t', '--toolchains', '-gt', '--global-toolchains') and i+1 < len(args):
            path = Path(args[i+1]); path = path if path.is_absolute() else root/path
            settings[f'argument-{i}'] = digest(path)
        elif arg.startswith(('--settings=', '--global-settings=', '--toolchains=', '--global-toolchains=')):
            path = Path(arg.split('=',1)[1]); path = path if path.is_absolute() else root/path
            settings[f'argument-{i}'] = digest(path)
        elif re_match := next((flag for flag in ('-gs','-gt','-s','-t') if arg.startswith(flag) and len(arg)>len(flag)), None):
            path = Path(arg[len(re_match):]); path = path if path.is_absolute() else root/path
            settings[f'argument-{i}'] = digest(path)
    environment = {k: os.environ.get(k) for k in ('MAVEN_ARGS','MAVEN_OPTS','MAVEN_HOME','M2_HOME','JAVA_HOME','JAVA_TOOL_OPTIONS','JDK_JAVA_OPTIONS')}
    return dict(poms_and_maven=files, settings=settings,
                environment_sha256=hashlib.sha256(json.dumps(environment,sort_keys=True).encode()).hexdigest())


if __name__ == '__main__':
    Path(sys.argv[2]).write_text(json.dumps(collect(sys.argv[1]), sort_keys=True))
