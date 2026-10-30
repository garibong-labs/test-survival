"""Cumulative losses with a fixed method denominator."""

FAILURES={'compile-fail','fail','error','timeout','absent'}


def dead_method(method, index):
    return dict(method, state='dead', message=f'not executed; failed at index {index}', flaky=False, lines=[])


def advance(alive, methods, *, evaluated=True):
    remaining=set(alive)
    lost=[]
    if evaluated:
        for row in methods:
            if row['id'] in remaining and row['state']!='pass':
                if row['state'] not in FAILURES:
                    raise ValueError('Unknown method state: '+row['state'])
                remaining.remove(row['id']);lost.append(dict(row))
    return remaining,lost
