"""Source and documentation available at the generation commit."""
from base64 import b64decode
from pathlib import Path, PurePosixPath
import re
import subprocess

from ...io import atomic_text


def declarations(adapter, directory, rows):
    inputs = directory/'prompts/method-rows.tsv'
    keys = [(r['class'], str(r['line']), r['signature']) for r in rows]
    atomic_text(inputs, ''.join('\t'.join(key)+'\n' for key in keys))
    root = adapter.folder(0)
    log = directory/'prompts/method-materials.tsv'
    helper = Path(__file__).with_name('java')/'TestPilotMaterials.java'
    result = adapter.runner.execute(['java', str(helper), str(root/'src/main/java'), str(inputs)],
                                    root, log, adapter.config['runner']['timeouts']['compile'])
    if result['exit_code']:
        raise RuntimeError('TestPilot method inventory failed: '+log.read_text(errors='replace')[-2000:])
    found = {}
    for line in log.read_text().splitlines():
        if line.startswith('M\t'):
            _, cls, position, signature, *fields = line.split('\t')
            found[cls, position, signature] = dict(zip(('header', 'definition', 'doc'),
                                                       (b64decode(f).decode() for f in fields), strict=True))
    for key in keys:
        if key not in found:
            raise ValueError(f'Unmatched methods.csv row: {key}')
    return [found[key] for key in keys]


def fenced_blocks(lines):
    fence = None
    for index, line in enumerate(lines):
        if fence is None:
            opening = re.fullmatch(r' {0,3}(`{3,}|~{3,})(.*)', line)
            if opening:
                fence, info = opening.groups()
                include = info.strip().lower() in ('', 'java')
                start = index + 1
        elif re.fullmatch(r' {0,3}'+fence[0]+'{'+str(len(fence))+r',}[ \t]*', line):
            if include:
                yield start, lines[start:index]
            fence = None


def snippets(repo, sha, names, max_snippets, snippet_lines):
    result = {name: [] for name in names}
    if not result or not max_snippets:
        return result
    git = ['git', '-C', str(repo)]
    tree = subprocess.check_output([*git, 'ls-tree', '-rz', sha])
    tracked = {}
    experiments = set()
    for entry in tree.split(b'\0'):
        if not entry:
            continue
        metadata, raw_path = entry.split(b'\t', 1)
        mode, _, oid = metadata.split()
        path = PurePosixPath(raw_path.decode(errors='surrogateescape'))
        if path.name == 'experiment.yaml':
            experiments.add(path.parent)
        parents = {p.lower() for p in path.parts[:-1]}
        if mode not in (b'100644', b'100755') or parents & {
                'experiments', 'experiment', 'target', 'build', 'out', 'dist', '.gradle', '.local', 'node_modules'}:
            continue
        markdown = path.suffix.lower() in ('.md', '.markdown')
        if markdown or (path.suffix == '.java' and parents & {'examples', 'example', 'demo', 'samples'}):
            tracked[str(path)] = (markdown, oid)
    call = re.compile(r'(?<![\w$])([\w$]+)\s*\(')
    needed = set(result)
    for path, (markdown, oid) in sorted(tracked.items()):
        if not needed:
            break
        if experiments.intersection(PurePosixPath(path).parents):
            continue
        lines = subprocess.check_output([*git, 'cat-file', 'blob', oid]).decode(errors='replace').splitlines()
        blocks = fenced_blocks(lines) if markdown else [(0, lines)]
        for start, block in blocks:
            seen = set()
            for index, line in enumerate(block):
                for name in dict.fromkeys(call.findall(line)):
                    if name not in needed or (markdown and name in seen):
                        continue
                    seen.add(name)
                    first = max(0, min(index - snippet_lines//2, len(block) - snippet_lines))
                    result[name].append(dict(file=path, line=start+first+1, call_line=start+index+1,
                                             text='\n'.join(block[first:first+snippet_lines])))
                    if len(result[name]) == max_snippets:
                        needed.remove(name)
    return result
