"""Cumulative losses with a fixed method denominator."""

FAILURES={'compile-fail','fail','error','timeout','absent'}


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


def unchanged_module(paths,module='.'):
    prefix=module.rstrip('/')+'/' if module!='.' else ''
    return not any(p==prefix+'pom.xml' or p.startswith(prefix+'src/') for p in paths)
