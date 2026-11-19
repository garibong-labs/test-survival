"""Per-test line evidence shared by batch baselines and policy execution."""
from ..io import read_csv, read_json, result_exists


def usable_lines(row):
    """A non-executing retry cannot supply coverage for a preserved verdict."""
    return 'lines' in row and row.get('remeasured_state') in (None, 'pass', 'fail')


def coordinates(exp, index, groups):
    """The denominator survives a failed per-test coverage JVM."""
    for g in groups:
        path = exp/f'results/coverage/{index}-{g}-lines.csv'
        if result_exists(path):
            return {(row['file'], int(row['line'])) for row in read_csv(path)}
    for g in groups:
        raw = read_json(exp/f'results/raw-coverage/{index}-{g}.json', {})
        if 'universe' in raw:
            return {tuple(line) for line in raw['universe']}
    raise ValueError(f'Missing production line universe at measured index {index}')


def projected_lines(row, universe):
    """Keep origin coordinates that still exist in the current executable universe."""
    lines = row.get('lines')
    if lines is None or universe is None:
        return lines
    return [line for line in lines if tuple(line) in universe]
