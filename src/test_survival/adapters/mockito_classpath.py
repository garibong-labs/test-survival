"""Identify the Mockito runtime in Maven project and runner classpaths."""
from pathlib import Path
import re

MOCKITO_LIBRARIES = ('mockito-core', 'mockito-junit-jupiter', 'byte-buddy', 'byte-buddy-agent', 'objenesis')


def artifact(jar):
    match = re.fullmatch(r'(.+?)-(\d[^/]*)\.jar', Path(jar).name)
    if match and not match[2].endswith(('-tests', '-sources', '-javadoc')):
        return match.groups()
    return None, None


def project_owns_mockito(names):
    return any(name in ('mockito-core', 'mockito-all') for name in names)
