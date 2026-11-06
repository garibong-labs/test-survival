"""Map actual Surefire invocations to source methods without inventing tests."""
from collections import Counter, defaultdict
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET

from ..config import canonical_hash, record_runner_inputs
from ..io import read_json, write_csv, write_json


def report_methods(directory, methods):
    by_class = defaultdict(list)
    for method in methods:
        by_class[method['class']].append(method)
    observed = defaultdict(list)
    unmapped = []
    counts = Counter(run=0, failed=0, skipped=0)
    for path in sorted(Path(directory).glob('TEST-*.xml')):
        for case in ET.parse(path).getroot().iter('testcase'):
            cls = case.get('classname') or path.name[5:-4]
            name = case.get('name', '')
            state = ('skipped' if case.find('skipped') is not None else
                     'fail' if case.find('failure') is not None or case.find('error') is not None else 'pass')
            counts['skipped' if state == 'skipped' else 'run'] += 1
            counts['failed'] += state == 'fail'
            # Console-style unique IDs also survive some Surefire reporters.
            output = '\n'.join(n.text or '' for n in case.findall('system-out'))
            unique = re.search(r'\[(?:method|test-template|test-factory):([^\]]+)\]', output)
            candidates = [m for m in by_class[cls] if re.match(re.escape(m['method'])+r'(?:\(|\[|$)', name)]
            if not candidates and unique:
                candidates = [m for m in by_class[cls] if re.match(re.escape(m['method'])+r'(?:\(|\[|$)', unique[1])]
            if not candidates:
                candidates = [m for m in by_class[cls] if m.get('display') and m['display'] in name]
                if candidates:
                    longest = max(len(m['display']) for m in candidates)
                    candidates = [m for m in candidates if len(m['display']) == longest]
            if len(candidates) != 1:
                unmapped.append(dict(report=path.name, **{'class':cls}, name=name, status=state,
                                     reason='ambiguous-source-method' if candidates else 'no-source-method'))
            else:
                observed[cls, candidates[0]['method']].append(state)
    return observed, unmapped, dict(counts)


def surefire_config(pom):
    """Literal module configuration is diagnostic only, never a candidate filter."""
    root = ET.parse(pom).getroot()
    for node in root.iter(): node.tag = node.tag.split('}')[-1]
    plugins = root.findall('./build/plugins/plugin')
    plugin = next((p for p in plugins if p.findtext('artifactId') == 'maven-surefire-plugin'), None)
    if plugin is None: return {}
    config = plugin.find('configuration')
    if config is None: return {}
    values = {key: config.findtext(key, '') for key in ('groups', 'excludedGroups')}
    for key in ('includes', 'excludes'):
        node = config.find(key)
        values[key] = [n.text or '' for n in node] if node is not None else []
    return values


def ant_match(path, pattern):
    if not pattern or '${' in pattern or pattern.startswith('%regex['): return False
    # Surefire matches compiled class paths, accepting .java patterns as aliases.
    pattern = pattern.replace('.java', '.class')
    expression = re.escape(pattern).replace(r'\*\*/', '(?:.*/)?').replace(r'\*', '[^/]*').replace(r'\?', '[^/]')
    return re.fullmatch(expression, path) is not None


def exclusion_reason(method, states, settings):
    annotations = method.get('annotations', '')
    if re.search(r'@(?:[\w.]+\.)?(?:Disabled|Ignore)\b', annotations): return 'disabled'
    if states: return 'skipped'
    tags = set(re.findall(r'@(?:[\w.]+\.)?Tag\s*\(\s*"([^"]+)"\s*\)', annotations))
    categories = re.findall(r'@(?:[\w.]+\.)?Category\s*\((.*?)\)', annotations)
    tags.update(re.findall(r'([\w.]+)\s*\.class', ' '.join(categories)))
    excluded = set(re.findall(r'[\w.]+', settings.get('excludedGroups', '')))
    # Only a comma-separated literal list supports this diagnostic inference.
    if re.fullmatch(r'[\w.,\s]*', settings.get('excludedGroups', '')) and tags & excluded:
        return 'tag-or-group-exclusion'
    included = settings.get('groups', '')
    if tags and re.fullmatch(r'[\w.,\s]+', included) and not tags & set(re.findall(r'[\w.]+', included)):
        return 'tag-or-group-exclusion'
    path = method['class'].replace('.', '/')+'.class'
    if any(ant_match(path, p) for p in settings.get('excludes', [])): return 'exclude-pattern'
    includes = settings.get('includes', [])
    if includes and all('${' not in p and not p.startswith('%regex[') for p in includes) and not any(ant_match(path, p) for p in includes):
        return 'include-pattern'
    return 'not-reported-by-build'


def discover(adapter, methods):
    exp, folder = adapter.exp, adapter.folder(0)
    anchor = read_json(exp/'results/anchor.json')
    # Mapping changes do not require executing the same project's test phase again.
    fingerprint = canonical_hash(dict(sha=anchor['sha'], runner=record_runner_inputs(adapter.config['runner']), module=adapter.config['project']['module']))
    output = exp/'results/dev-build-discovery.json'
    previous = read_json(output, {})
    destination = exp/'results/dev-build-reports'
    if previous.get('input_sha256') == fingerprint and previous.get('complete') and destination.is_dir():
        result = previous
    else:
        reports = folder/'target/surefire-reports'
        if reports.exists(): shutil.rmtree(reports)
        if destination.exists(): shutil.rmtree(destination)
        module = adapter.config['project']['module']
        command = ['mvn', '-o', 'test', '-Dmaven.test.failure.ignore=true']
        cwd = folder
        if module not in ('.', './'):
            command += ['-pl', module, '-am']
            cwd = exp/'work/checkouts/0'
        result = adapter.runner.execute(command, cwd, exp/'results/dev-build-test-phase.log', adapter.config['runner']['timeouts']['build'])
        result.update(input_sha256=fingerprint, sha=anchor['sha'], complete=False)
        write_json(output, result)
        if reports.exists(): shutil.copytree(reports, destination)
        # Test failures are ignored by Maven; a nonzero exit is infrastructure or compilation failure.
        if result['exit_code'] or result.get('timeout'):
            raise RuntimeError('Build test-phase discovery failed; see results/dev-build-test-phase.log')
        destination.mkdir(parents=True, exist_ok=True)
    observed, unmapped, counts = report_methods(destination, methods)
    settings = surefire_config(folder/'pom.xml')
    candidates, excluded = [], []
    for method in methods:
        states = observed.get((method['class'], method['method']), [])
        if any(s != 'skipped' for s in states): candidates.append(method)
        else:
            ambiguous = any(r['reason']=='ambiguous-source-method' and r['class']==method['class'] and
                            (re.match(re.escape(method['method'])+r'(?:\(|\[|$)', r['name']) or
                             method.get('display') and method['display'] in r['name']) for r in unmapped)
            excluded.append({**{k:method[k] for k in ('class','method','kind')},
                             'reason':'ambiguous-source-method' if ambiguous else exclusion_reason(method, states, settings)})
    write_csv(exp/'dev/dev-not-run-by-build.csv', excluded, ['class','method','kind','reason'])
    write_csv(exp/'dev/build-report-unmapped.csv', unmapped, ['report','class','name','status','reason'])
    result.update(complete=True, counts=dict(counts, methods_run=len(candidates), excluded=len(excluded), unmapped=len(unmapped)),
                  excluded_by_reason=dict(Counter(r['reason'] for r in excluded)),
                  inherited_methods_run=sum(bool(m.get('declaring_class')) for m in candidates),
                  unmapped_by_reason=dict(Counter(r['reason'] for r in unmapped)),
                  reason_basis='Skipped reports and literal module Surefire configuration/source annotations; otherwise not-reported-by-build')
    write_json(output, result)
    anchor['dev_build_discovery'] = result
    write_json(exp/'results/anchor.json', anchor)
    return candidates
