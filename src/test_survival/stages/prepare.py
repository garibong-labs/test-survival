from concurrent.futures import ThreadPoolExecutor

from ..adapters.java_maven import JavaMavenAdapter
from ..io import read_json, read_csv, write_json, write_csv, atomic_text, now
from ..pipeline import log
from ..config import range_fingerprint
from ..rules.commits import skip_reason


def run(exp, config):
    adapter = JavaMavenAdapter(exp, config)
    prior = read_json(exp/'results/anchor.json', {})
    anchor_sha = adapter.git('rev-parse', config['anchor']+'^{commit}')
    resolution_hash = range_fingerprint(config)
    resolution = read_json(exp/'state/resolved-range.json', {})
    saved_end = resolution.get('end') if resolution.get('config_hash') == resolution_hash else None
    end = config['range']['end'] or saved_end or adapter.git('rev-parse', config['range']['branch'])
    end = adapter.git('rev-parse', end+'^{commit}')
    first_parents = adapter.git('rev-list', '--first-parent', end).splitlines()
    if anchor_sha not in first_parents:
        descendants = set(adapter.git('rev-list', '--ancestry-path', anchor_sha+'..'+end).splitlines())
        containing = next((sha for sha in reversed(first_parents) if sha in descendants), None)
        message = 'anchor is not on the range endpoint first-parent history'
        if containing:
            module = config['project']['module'].removeprefix('./').rstrip('/')
            identical = not adapter.git('diff', '--no-ext-diff', '--no-renames', '--name-only', anchor_sha, containing, '--', module or '.')
            message += f'; first first-parent commit containing anchor: {containing}; module tree identical to anchor: {"yes" if identical else "no"}; set anchor to {containing}'
        else:
            message += '; no first-parent commit at this endpoint contains the anchor'
        raise RuntimeError(message)
    write_json(exp/'state/resolved-range.json', {'config_hash':resolution_hash, 'end':end})
    shas = adapter.git('rev-list', '--first-parent', '--reverse', anchor_sha+'..'+end).splitlines()
    if config['range']['limit'] is not None:
        shas = shas[:config['range']['limit']]
    anchor = dict(tag=config['anchor'], sha=anchor_sha, origin_master=end, n=len(shas), started=prior.get('started', now()),
                  date=adapter.git('show','-s','--format=%cI',anchor_sha), deviations=[], key_check='KEY_MISSING', dev_unmapped_excluded=0)
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
    anchor_build = adapter.build(0, anchor_sha)['build']
    log(exp, f'prepare: anchor build {anchor_build}', config)
    if anchor_build != 'ok':
        raise RuntimeError('Anchor build failed')
    imported = read_json(exp/'state/import.json', {})
    from ..dev_results import imported_baseline
    if not imported.get('dev-baseline') and not imported_baseline(exp, config):
        baseline = adapter.baseline()
        log(exp, f'prepare: baseline has {sum(r["stable"]=="yes" for r in baseline)} stable methods', config)
    excluded_path = exp/'dev/dev-unmapped-excluded.csv'
    anchor['dev_unmapped_excluded'] = len(read_csv(excluded_path)) if excluded_path.exists() else 0
    from ..rules.dev_denominator import unmapped_note
    note = unmapped_note(anchor['dev_unmapped_excluded'])
    if note:
        anchor['deviations'].append(note)
        log(exp, 'prepare: '+note, config)
    write_json(exp/'results/anchor.json', anchor)
    def build(row):
        reason=skip_reason(touches[str(row['index'])],config['project']['module'],config['range']['skip_untouched'])
        if reason:
            result=dict(index=row['index'],sha=row['sha'],build='skipped-untouched',attempts=[],reason=reason)
            write_json(exp/f'results/builds/{row["index"]}.json',result)
            log(exp, f'prepare: build {row["index"]}/{len(commits)} skipped-untouched', config)
            return result
        result = adapter.build(row['index'], row['sha'])
        log(exp, f'prepare: build {row["index"]}/{len(commits)} {result["build"]}', config)
        return result
    adapter.setup_runner()
    with ThreadPoolExecutor(max_workers=config['runner']['workers']['build']) as pool:
        builds = list(pool.map(build, commits))
    atomic_text(exp/'results/commits-check.md', '# Complete range build check\n\n'+f'{len(commits)} first-parent commits; {sum(r["build"]=="ok" for r in builds)} built successfully. Build failures carry forward survival and are excluded from evaluation.\n')
    return f'{len(commits)} commits; anchor {anchor_sha}; builds recorded'+('; '+note if note else '')
