"""Chronological selection from durable method evidence and endpoint diffs."""
from functools import lru_cache
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

from .io import read_json
from .rules.coverage import usable_lines
from .adapters.dependencies import RECORD_VERSION

FIELDS = ('selection_action', 'carried_from_index', 'last_executed_index', 'last_executed_sha', 'dependency_record')


def affected(config): return config['survival'].get('selection', 'all') == 'affected'


def compiler_pom(text):
    if text is None: return (), (), ()
    root = ET.fromstring(text)
    for element in root.iter(): element.tag = element.tag.split('}')[-1]
    def shape(node):
        return (node.tag, (node.text or '').strip(), tuple(sorted(node.attrib.items())), tuple(shape(c) for c in node))
    parents = [shape(v) for p in root.iter('parent') for v in p.findall('version')]
    plugins = [shape(p) for p in root.iter('plugin') if p.findtext('artifactId') == 'maven-compiler-plugin']
    properties = [shape(p) for ps in root.iter('properties') for p in ps if re.search(r'compiler|release|source|target|java\.version', p.tag, re.I)]
    return tuple(parents), tuple(plugins), tuple(properties)


def reason(row, index, sha, context, changed, compiler_changed, *, start=0):
    if not row or row.get('state') != 'pass' or not usable_lines(row): return 'missing observation'
    deps = row.get('dependency_record', {})
    previous = row.get('last_executed_index')
    if (deps.get('version') != RECORD_VERSION or deps.get('selection') != 'affected' or previous is None
            or previous < start or previous >= index or not row.get('last_executed_sha')):
        return 'missing segment dependencies'
    if row.get('remeasured_state') not in (None, row.get('state')): return 'disagreeing repair'
    if deps.get('unbounded', True): return 'unbounded dependencies'
    if not isinstance(deps.get('files'), list): return 'missing dependency files'
    if not context.get('known') or not deps.get('context', {}).get('known'): return 'unknown classpath'
    if context['entries'] != deps['context']['entries']: return 'classpath changed'
    if compiler_changed: return 'compiler POM changed'
    if set(deps['files']) & set(changed): return 'dependency changed'
    if any(p.startswith('src/main/') and not p.startswith('src/main/java/') for p in changed): return 'production resource changed'
    return None


def carry(row):
    return {**row, 'selection_action':'carried', 'carried_from_index':row['last_executed_index']}


def executed(row, index, sha, context=None):
    result = {k:v for k,v in row.items() if k not in ('carried_from_index',)}
    result.update(selection_action='executed', last_executed_index=index, last_executed_sha=sha)
    if 'dependency_record' in result:
        result['dependency_record'] = {**result['dependency_record'], 'context':context or {}}
    return result


def counts(rows):
    result = dict(executed=0, carried=0)
    for row in rows:
        action = row.get('selection_action')
        if action in result and row.get('state') not in ('dead','not-selected'):
            result[action] += 1
    return result


class Selector:
    def __init__(self, adapter):
        self.adapter = adapter
        self.repo = Path(adapter.config['project']['repo'])
        self.module = adapter.config['project']['module'].removeprefix('./').strip('/')

    def git(self, *args):
        return subprocess.check_output(['git','-C',str(self.repo),*args])

    @lru_cache(maxsize=1024)
    def interval(self, before, after):
        paths = self.git('diff','--no-ext-diff','--no-renames','--name-only','-z',before,after).decode().split('\0')
        compiler = False
        for path in paths:
            if Path(path).name != 'pom.xml': continue
            values = []
            for sha in (before, after):
                result = subprocess.run(['git','-C',str(self.repo),'show',sha+':'+path],capture_output=True)
                values.append(compiler_pom(result.stdout.decode()) if result.returncode == 0 else compiler_pom(None))
            compiler |= values[0] != values[1]
        prefix = self.module+'/' if self.module not in ('','.') else ''
        return [p[len(prefix):] for p in paths if p and p.startswith(prefix)], compiler

    @lru_cache(maxsize=128)
    def context(self, index, sha):
        path = f'results/selection-context/{index}.json'
        existing = read_json(self.adapter.exp/path, {})
        if existing.get('sha') == sha and existing.get('version') == 1: return existing
        return self.adapter.worker(dict(action='selection-context', index=index, sha=sha, methods=[],
                 work=f'work/selection-context/{index}', output=path))

    def decision(self, row, index, sha, context, start=0):
        if not row or not row.get('last_executed_sha'): return 'missing observation'
        try: changed, compiler = self.interval(row['last_executed_sha'], sha)
        except (OSError, ValueError, ET.ParseError, subprocess.CalledProcessError): return 'unknown diff'
        return reason(row, index, sha, context, changed, compiler, start=start)
