"""Decide which indexed commits cannot affect frozen-test outcomes."""
from pathlib import PurePosixPath


def coverage_indices(commits, skipped, coverage):
    """Plan coverage consumers, including aliases past untouched predecessors."""
    indices = {0} if coverage['at_anchor'] else set()
    aliases = {}
    if coverage['changed_line']:
        for row in commits:
            if row['changes-src-main'] != 'yes':
                continue
            index = int(row['index'])
            previous = index - 1
            while previous in skipped:
                previous -= 1
            if previous != index - 1:
                aliases[str(index - 1)] = previous
            indices.update((index, previous))
    return indices - set(skipped), aliases


def skip_reason(paths, module, mode):
    if mode == 'none':
        return None
    if mode not in ['module', 'main-sources']:
        raise ValueError('range.skip_untouched: expected none, module, or main-sources')
    module=str(PurePosixPath(module))
    prefix='' if module=='.' else module.rstrip('/')+'/'
    watched=prefix+('src/main/' if mode=='main-sources' else '')
    if any(PurePosixPath(path).name=='pom.xml' or path.startswith(watched) for path in paths):
        return None
    return 'No changes under '+(watched or './')+' or to any pom.xml'
