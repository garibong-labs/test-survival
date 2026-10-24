"""Decide which indexed commits cannot affect frozen-test outcomes."""
from pathlib import PurePosixPath


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
