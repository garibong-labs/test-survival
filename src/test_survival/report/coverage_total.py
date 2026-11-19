"""Endpoint coverage shared by baseline and policy reports."""
from ..io import atomic_text, read_csv, result_exists, write_csv
from ..rules.coverage import coordinates


FIELDS = ['index', 'sha', 'group', 'files', 'executable_lines', 'covered_lines', 'percent']


def write(directory, rows):
    write_csv(directory/'coverage-total.csv', rows, FIELDS)
    table = ['# Production line coverage', '',
             '| Index | SHA | Group | Files | Executable lines | Covered lines | Percent |',
             '|---:|---|---|---:|---:|---:|---:|']
    for row in rows:
        table.append('| '+' | '.join(str(row[k]) if row[k] != '' else '—' for k in FIELDS)+' |')
    atomic_text(directory/'coverage-total.md', '\n'.join(table)+'\n')


def baseline(exp, groups, endpoints):
    rows = []
    for index, sha in endpoints:
        universe = coordinates(exp, index, groups)
        files = len({file for file, _ in universe})
        for group in groups:
            path = exp/f'results/coverage/{index}-{group}-lines.csv'
            covered = sum(r['covered'] == 'yes' for r in read_csv(path)) if result_exists(path) else ''
            rows.append(dict(index=index, sha=sha, group=group, files=files, executable_lines=len(universe),
                             covered_lines=covered, percent=round(100*covered/len(universe), 3)
                             if universe and covered != '' else ''))
    write(exp/'results', rows)
