from concurrent.futures import ThreadPoolExecutor

from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json, write_json, write_csv, atomic_text, now
from ..pipeline import log
from ..config import canonical_hash
from ..rules.commits import skip_reason


def run(exp, config):
    adapter = JavaMavenAdapter(exp, config)
    prior = read_json(exp/'results/anchor.json', {})
    anchor_sha = adapter.git('rev-parse', config['anchor']+'^{commit}')
    resolution_hash = canonical_hash({k:config[k] for k in ['project','anchor','range']})
    resolution = read_json(exp/'state/resolved-range.json', {})
    saved_end = resolution.get('end') if resolution.get('config_hash') == resolution_hash else None
    end = config['range']['end'] or saved_end or adapter.git('rev-parse', config['range']['branch'])
    end = adapter.git('rev-parse', end+'^{commit}')
    if anchor_sha not in adapter.git('rev-list', '--first-parent', end).splitlines():
        raise RuntimeError('anchor is not on the range endpoint first-parent history')
    write_json(exp/'state/resolved-range.json', {'config_hash':resolution_hash, 'end':end})
    shas = adapter.git('rev-list', '--first-parent', '--reverse', anchor_sha+'..'+end).splitlines()
    if config['range']['limit'] is not None:
        shas = shas[:config['range']['limit']]
    anchor = dict(tag=config['anchor'], sha=anchor_sha, origin_master=end, n=len(shas), started=prior.get('started', now()),
                  date=adapter.git('show','-s','--format=%cI',anchor_sha), deviations=[], key_check='KEY_MISSING')
    # No generation is needed for prepare. Credential presence is probed without returning its value.
    from ..model.credentials import key_available
    if key_available(config['model']):
        anchor['key_check'] = 'KEY_OK (value never logged)'
    for g in config['generators']:
        if g['method'] == 'ktester':
            import subprocess
            anchor['ktester_base'] = subprocess.check_output(['git','-C',g['tool_path'],'rev-parse','HEAD'], text=True).strip()
            break
    write_json(exp/'results/anchor.json', anchor)
    commits, touches = [], {}
    module = config['project']['module']
    module = '' if module == '.' else module.removeprefix('./').rstrip('/')
    prefix = module+'/' if module else ''
    for index, sha in enumerate(shas, 1):
        paths = adapter.git('diff','--no-renames','--name-only',sha+'^1',sha).splitlines()
        touches[str(index)] = paths
        date, subject = adapter.git('show','-s','--format=%cI%n%s',sha).split('\n',1)
        commits.append(dict(index=index, sha=sha, date=date, subject=subject,
                            **{'changes-src-main': 'yes' if any(p.startswith(prefix+'src/main/java/') for p in paths) else 'no',
                               'changes-pom': 'yes' if prefix+'pom.xml' in paths else 'no'}))
    write_csv(exp/'results/commits.csv',commits,['index','sha','date','subject','changes-src-main','changes-pom'])
    write_json(exp/'results/commit-paths.json', touches)
    if adapter.build(0, anchor_sha)['build'] != 'ok':
        raise RuntimeError('Anchor build failed')
    imported = read_json(exp/'state/import.json', {})
    if not imported.get('dev-baseline'):
        baseline = adapter.baseline()
        log(exp, f'prepare: baseline has {sum(r["stable"]=="yes" for r in baseline)} stable methods')
    def build(row):
        reason=skip_reason(touches[str(row['index'])],config['project']['module'],config['range']['skip_untouched'])
        if reason:
            result=dict(index=row['index'],sha=row['sha'],build='skipped-untouched',attempts=[],reason=reason)
            write_json(exp/f'results/builds/{row["index"]}.json',result)
            return result
        result = adapter.build(row['index'], row['sha'])
        log(exp, f'prepare: build {row["index"]}/{len(commits)} {result["build"]}')
        return result
    with ThreadPoolExecutor(max_workers=config['runner']['workers']['build']) as pool:
        builds = list(pool.map(build, commits))
    atomic_text(exp/'results/commits-check.md', '# Complete range build check\n\n'+f'{len(commits)} first-parent commits; {sum(r["build"]=="ok" for r in builds)} built successfully. Build failures carry forward survival and are excluded from evaluation.\n')
    return f'{len(commits)} commits; anchor {anchor_sha}; builds recorded'
