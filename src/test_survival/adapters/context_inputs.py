"""Container-side content identity; persist digests, never Maven credentials."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import re
import uuid
import xml.etree.ElementTree as ET


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def activation_inputs(root, models, properties):
    """Bound file activation and referenced environment values without an effective POM.

    An unresolved interpolation cannot safely share a cache key. Do not persist
    property values (settings can contain credentials), only existence and hashes.
    """
    exists, environment, unknown = {}, {}, []
    for label, path, bases in models:
        if not path.is_file(): continue
        text = path.read_text()
        names = set(re.findall(r'\$\{env\.([^}]+)\}', text))
        try:
            tree = ET.fromstring(text)
            for node in tree.iter(): node.tag = node.tag.split('}')[-1]
            names.update(n.text.strip().removeprefix('!')[4:] for n in tree.findall('.//activation/property/name')
                         if (n.text or '').strip().removeprefix('!').startswith('env.'))
            for i, node in enumerate(tree.findall('.//activation/property/name')):
                name = (node.text or '').strip().removeprefix('!')
                if not name.startswith('env.') and name not in properties:
                    # Maven also exposes JVM system properties. Without their
                    # effective value, absence cannot be assumed (e.g. user.dir).
                    unknown.append(f'{label}:property:{i}')
            for i, node in enumerate(tree.findall('.//activation/file/*')):
                if node.tag not in ('exists', 'missing'): continue
                for base_number, base in enumerate(bases):
                    values = {**properties, 'basedir':str(base), 'project.basedir':str(base),
                              'maven.multiModuleProjectDirectory':str(root)}
                    def interpolate(match):
                        key = match[1]
                        return os.environ.get(key[4:], match[0]) if key.startswith('env.') else values.get(key, match[0])
                    value = re.sub(r'\$\{([^}]+)\}', interpolate, (node.text or '').strip())
                    key = f'{label}:{i}:{base_number}:{node.tag}'
                    if not value or '${' in value:
                        unknown.append(key)
                    else:
                        target = Path(value)
                        exists[key] = (target if target.is_absolute() else base/target).exists()
        except ET.ParseError:
            unknown.append(label)
        environment.update({name:os.environ.get(name) for name in names})
    return dict(files=exists, environment_sha256=hashlib.sha256(json.dumps(environment,sort_keys=True).encode()).hexdigest(),
                unbounded=unknown, nonce=uuid.uuid4().hex if unknown else None)


def collect(root, invocation=None):
    root = Path(root).resolve()
    invocation = Path(invocation).resolve() if invocation is not None else root
    # Maven's launcher uses the nearest ancestor .mvn, starting at its actual
    # working directory, unless MAVEN_BASEDIR explicitly overrides that search.
    ancestors = [invocation, *invocation.parents]
    override = os.environ.get('MAVEN_BASEDIR')
    maven_base = ((invocation/override).resolve() if override else
                  next((p for p in ancestors if (p/'.mvn').is_dir()), invocation))
    files = {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob('pom.xml'))}
    configs = {root, maven_base, *(p for p in ancestors if p.is_relative_to(root))}
    for directory in sorted(configs):
        for p in sorted((directory/'.mvn').rglob('*')):
            if p.is_file():
                label = str(p.relative_to(root)) if p.is_relative_to(root) else 'external-maven-base/'+str(p.relative_to(maven_base))
                files[label] = digest(p)
    executable = Path(shutil.which('mvn') or '/missing-mvn').resolve()
    jvm_config = maven_base/'.mvn/jvm.config'
    jvm_args = ' '.join(os.environ.get(k, '') for k in ('JAVA_TOOL_OPTIONS','JDK_JAVA_OPTIONS'))
    if jvm_config.is_file(): jvm_args += ' '+jvm_config.read_text()
    jvm_args += ' '+os.environ.get('MAVEN_OPTS', '')
    properties = {arg[2:].split('=',1)[0]:arg.split('=',1)[1] if '=' in arg else ''
                  for arg in shlex.split(jvm_args) if arg.startswith('-D')}
    user_home = Path(properties.get('user.home', str(Path.home())))
    if not user_home.is_absolute(): user_home = invocation/user_home
    global_home = Path(os.environ.get('MAVEN_HOME') or os.environ.get('M2_HOME') or executable.parent.parent)
    settings_paths = {label:p for label,p in (('user', user_home/'.m2/settings.xml'),
        ('global', global_home/'conf/settings.xml'), ('user-toolchains', user_home/'.m2/toolchains.xml'),
        ('global-toolchains', global_home/'conf/toolchains.xml'), ('security', user_home/'.m2/settings-security.xml'))}
    config = maven_base/'.mvn/maven.config'
    args = shlex.split(os.environ.get('MAVEN_ARGS', '')) + shlex.split(config.read_text() if config.is_file() else '')
    for i, arg in enumerate(args):
        if arg in ('-s', '--settings', '-gs', '--global-settings', '-t', '--toolchains', '-gt', '--global-toolchains') and i+1 < len(args):
            path = Path(args[i+1]); path = path if path.is_absolute() else invocation/path
            settings_paths[f'argument-{i}'] = path
        elif arg.startswith(('--settings=', '--global-settings=', '--toolchains=', '--global-toolchains=')):
            path = Path(arg.split('=',1)[1]); path = path if path.is_absolute() else invocation/path
            settings_paths[f'argument-{i}'] = path
        elif re_match := next((flag for flag in ('-gs','-gt','-s','-t') if arg.startswith(flag) and len(arg)>len(flag)), None):
            path = Path(arg[len(re_match):]); path = path if path.is_absolute() else invocation/path
            settings_paths[f'argument-{i}'] = path
    properties.update({arg[2:].split('=',1)[0]:arg.split('=',1)[1] if '=' in arg else ''
                       for arg in args if arg.startswith('-D')})
    properties.setdefault('user.home', str(user_home))
    poms = sorted(root.rglob('pom.xml'))
    models = [(str(p.relative_to(root)), p, [p.parent]) for p in poms]
    models += [('settings-'+label, p, [m.parent for m in poms]) for label,p in settings_paths.items()]
    activation = activation_inputs(maven_base, models, properties)
    environment = {k: os.environ.get(k) for k in ('MAVEN_ARGS','MAVEN_OPTS','MAVEN_BASEDIR','MAVEN_HOME','M2_HOME','JAVA_HOME','JAVA_TOOL_OPTIONS','JDK_JAVA_OPTIONS')}
    base_identity = (str(maven_base.relative_to(root)) if maven_base.is_relative_to(root) else
                     'external:'+hashlib.sha256(str(maven_base).encode()).hexdigest())
    return dict(invocation=str(invocation.relative_to(root)), maven_base=base_identity, poms_and_maven=files,
                settings={label:digest(p) for label,p in settings_paths.items()}, activation=activation,
                environment_sha256=hashlib.sha256(json.dumps(environment,sort_keys=True).encode()).hexdigest())


if __name__ == '__main__':
    Path(sys.argv[3]).write_text(json.dumps(collect(sys.argv[1], sys.argv[2]), sort_keys=True))
