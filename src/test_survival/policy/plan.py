"""Deterministic planning: no checkout, build, or model invocation."""
import subprocess
from pathlib import PurePosixPath

from ..config import ConfigError, canonical_hash
from ..io import atomic_text, write_json, write_csv
from .baseline import Baseline


def release_index(commits, tag_sha, contains):
    return next((int(r['index']) for r in commits if contains(tag_sha, r['sha'])), None)


def threshold_indices(curves, group, denominator, threshold, maximum=1):
    points = []
    start_alive = denominator
    for row in curves:
        alive = int(row[group+'_alive'])
        if int(row['index']) and start_alive > 0 and start_alive - alive >= threshold * start_alive:
            if len(points) >= maximum:
                break
            points.append(int(row['index']))
            start_alive = alive
    return points


def next_evaluable(index, indices, evaluable):
    return next((i for i in indices if i >= index and evaluable(i)), None)


def build_plan(baseline):
    b = baseline
    indices = [int(r['index']) for r in b.commits]
    releases = []
    def contains(tag, sha):
        result = subprocess.run(['git', '-C', str(b.repo), 'merge-base', '--is-ancestor', tag, sha], capture_output=True)
        if result.returncode not in (0, 1):
            raise ConfigError('Cannot resolve release ancestry')
        return result.returncode == 0
    for tag in b.options['releases']:
        sha = b.git('rev-parse', '--verify', 'refs/tags/'+tag+'^{commit}')
        index = release_index(b.commits, sha, contains)
        releases.append(dict(tag=tag, tag_sha=sha, requested_index=index))
    points = []
    prefix = str(PurePosixPath(b.config['project']['module']))
    prefix = '' if prefix == '.' else prefix+'/'
    files_at = {}
    for group in b.groups:
        thresholds = threshold_indices(b.curves, group, len(b.frozen[group]), b.options['threshold'], b.options['max_threshold_triggers'])
        for trigger, candidates in [('release', releases), ('threshold', [dict(requested_index=i) for i in thresholds])]:
            for candidate in candidates:
                requested = candidate['requested_index']
                index = next_evaluable(requested, indices, b.evaluable) if requested is not None else None
                point = dict(group=group, trigger=trigger, **candidate, index=index)
                if index is not None:
                    row = b.by_index[index]
                    files_at.setdefault(index, None)
                    if files_at[index] is None:
                        files_at[index] = set(b.git('ls-tree', '-r', '--name-only', row['sha']).splitlines())
                    point.update(sha=row['sha'], baseline_alive=int(row[group+'_alive']), baseline_denominator=len(b.frozen[group]),
                                 missing_classes=[c['class'] for c in b.scope['classes'] if prefix+c['file'] not in files_at[index]])
                else:
                    point['reason'] = 'release outside range' if requested is None else 'no later evaluable commit'
                points.append(point)
    needed_triggers = {p['trigger'] for p in b.options['set']}
    pools = {(p['group'], p['index']): {k: p[k] for k in ('group', 'index', 'sha', 'missing_classes')}
             for p in points if p['index'] is not None and p['trigger'] in needed_triggers}
    return dict(version=1, baseline=str(b.path), groups=b.groups, policies=b.options['set'], points=points,
                pools=[pools[key] for key in sorted(pools)],
                denominators={g: len(b.frozen[g]) for g in b.groups},
                threshold_basis='baseline survival; after a trigger, remaining baseline alive is the planning segment denominator')


def markdown(plan):
    lines = ['# Regeneration plan', '', f'Baseline (read-only): `{plan["baseline"]}`', '',
             '| Group | Trigger | Tag | Requested index | Evaluable index | Baseline alive / denominator | Missing scope classes |',
             '|---|---|---|---:|---:|---:|---|']
    for p in plan['points']:
        lines.append(f'| {p["group"]} | {p["trigger"]} | {p.get("tag", "")} | {p["requested_index"]} | {p["index"]} | '
                     + (f'{p["baseline_alive"]} / {p["baseline_denominator"]}' if p['index'] is not None else p['reason'])
                     + ' | '+', '.join(p.get('missing_classes', []))+' |')
    for g in plan['groups']:
        if not any(p['group'] == g and p['trigger'] == 'threshold' for p in plan['points']):
            lines.append(f'\n{g}: threshold never reached (or triggers disabled).')
    lines.extend(['', '## Distinct pools', ''])
    lines.extend(f'- `{p["group"]}/{p["index"]}` at `{p["sha"]}`' for p in plan['pools'])
    return '\n'.join(lines)+'\n'


def run(exp, config):
    b = Baseline(exp, config)
    plan = build_plan(b)
    output = b.guard(b.exp/'results/policy')
    write_json(output/'plan.json', plan)
    atomic_text(output/'plan.md', markdown(plan))
    write_json(b.exp/'results/commit-paths.json', b.commit_paths())
    write_csv(b.exp/'results/skipped-commits.csv', [dict(index=i, sha=b.by_index[i]['sha'], reason=reason) for i, reason in b.skipped.items()], ['index', 'sha', 'reason'])
    write_json(b.exp/'state/policy-plan.json', dict(status='completed', config_hash=canonical_hash(dict(options=b.options, plan=plan))))
    return plan
