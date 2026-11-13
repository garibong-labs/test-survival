"""Explicit generated-denominator exclusions, independent of test execution."""
from pathlib import PurePosixPath
import re


NAME = r'[A-Za-z_$][A-Za-z0-9_$]*'
CLASS = NAME + r'(?:\.' + NAME + r')*'


def valid_selector(selector):
    if selector.endswith('.java'):
        path = PurePosixPath(selector)
        return (not path.is_absolute() and str(path) == selector
                and '..' not in path.parts and not re.search(r'[\\*?\[\]#\s\x00]', selector))
    return re.fullmatch(CLASS + r'(?:#' + NAME + r')?', selector) is not None


def matches(selector, test, group, method):
    if selector.endswith('.java'):
        paths = {test['file']}
        # The author tool's retained source copy and its screened copy have
        # different paths. Normalize only this known layout, not arbitrary suffixes.
        if method == 'ktester':
            parts = PurePosixPath(test['file']).parts
            if len(parts) == 6 and parts[:2] == ('generated', group) and parts[4] == 'test_classes':
                paths.add(str(PurePosixPath(*parts[:3], 'sources', parts[-1])))
        return selector in paths
    cls, separator, name = selector.partition('#')
    return test.get('class') == cls and (not separator or test.get('method') == name)
