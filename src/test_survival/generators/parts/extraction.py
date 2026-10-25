"""Part 2 delimiter-first extraction; no source repair or normalization."""
import csv
import hashlib
import json
from pathlib import Path
import re

TECHS = ['ZSL', 'FSL', 'CoT', 'ToT', 'GToT']
MARKER = re.compile(r'^\s*(?:\*\*|__|`)?###Test (START|END)#{2,3}(?:\*\*|__|`)?\s*$')


def lexical_masks(text):
    """Mask comments, then comments and literals, preserving offsets."""
    comments, code = list(text), list(text)
    i = 0
    while i < len(text):
        start = i
        if text.startswith('//', i):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end
            comment = True
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            i = len(text) if end < 0 else end + 2
            comment = True
        elif text[i] in '\"\'':
            quote = text[i]
            i += 1
            while i < len(text):
                if text[i] == '\\':
                    i += 2
                elif text[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            i = min(i, len(text))
            comment = False
        else:
            i += 1
            continue
        for j in range(start, i):
            if text[j] not in '\r\n':
                code[j] = ' '
                if comment:
                    comments[j] = ' '
    return ''.join(comments), ''.join(code)


def extract(text, csr_success=None):
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines)
                  if (m := MARKER.fullmatch(line)) and m[1] == 'START'), None)
    end = next((i for i in range(start + 1, len(lines))
                if (m := MARKER.fullmatch(lines[i])) and m[1] == 'END'), None) if start is not None else None
    paired = start is not None and end is not None
    selected, source = [], None
    if paired:
        selected, source = lines[start + 1:end], 'markers'
    else:
        blocks, block = [], None
        for line in lines:
            if line.startswith('```'):
                if block is None:
                    block = []
                else:
                    candidate = ''.join(block)
                    if '@Test' in candidate or 'import org.junit' in candidate:
                        blocks.append(block)
                    block = None
            elif block is not None:
                block.append(line)
        if blocks:
            selected = max(blocks, key=lambda b: len(''.join(b)))
            source = 'fence-fallback'
    combined = ''.join(line for line in selected if not line.startswith('```'))
    comments, code = lexical_masks(combined)
    depth, balanced, depths = 0, True, []
    for char in code:
        depths.append(depth)
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth < 0:
                balanced = False
    balanced = balanced and depth == 0
    public = next((m for m in re.finditer(r'\bpublic\s+class\s+(\w+)', code)
                   if depths[m.start()] == 0), None)
    name = public or re.search(r'\bclass\s+(\w+)', code)
    package = re.search(r'\bpackage\s+([\w.]+)\s*;', code)
    class_name = name[1] if name else None
    pkg = package[1] if package else ''
    junit = '@Test' in combined or 'import org.junit' in combined
    html = bool(re.search(r'<(?:html|body|div|script)\b', code, re.I))
    # A truncated response can visibly contain code without a complete extractable block.
    detected = bool(combined.strip()) or bool(re.search(r'\bclass\s+\w+', text) and
                                              ('@Test' in text or 'import org.junit' in text))
    return dict(msr_detected=detected, csr_v2=bool(name and junit and balanced and not html),
                combine_v2=combined, class_name=class_name, package=pkg,
                fqcn=(pkg + '.' if pkg else '') + class_name if class_name else None,
                csr_success=csr_success, selection=source, marker_pair=paired,
                braces_balanced=balanced, html_detected=html)

