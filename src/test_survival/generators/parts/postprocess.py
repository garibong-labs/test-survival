from ...io import read_json


def repair(adapter,directory,passes):
    types=read_json(directory/'prompts/type-inventory.json')
    if types is None:raise RuntimeError('Import repair requires the project type inventory')
    relative=str(directory.relative_to(adapter.exp))
    return adapter.worker(dict(action='repair',directory=relative,types=types,passes=passes,
                               work='work/import-repair/'+relative,output=relative+'/import-fixes.json'))
