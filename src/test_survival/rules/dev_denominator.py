"""Stable developer populations; class-name matching follows the archived rule."""

def denominator(methods, mode, scope, production_names):
    def targeted(cls):
        pkg, _, simple = cls.rpartition('.')
        for selected in scope:
            cp, _, name = selected['class'].rpartition('.')
            if pkg != cp or not simple.startswith(name) or not simple.endswith('Test'):
                continue
            suffix = simple[len(name):-4]
            if not suffix or suffix not in production_names:
                return True
        return False
    return [{k:r[k] for k in ('class','method','kind')} for r in methods
            if r['stable']=='yes' and (mode=='project' or targeted(r['class']))]
