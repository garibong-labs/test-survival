"""Per-test line evidence shared by batch baselines and policy execution."""


def usable_lines(row):
    """A non-executing retry cannot supply coverage for a preserved verdict."""
    return 'lines' in row and row.get('remeasured_state') in (None, 'pass', 'fail')
