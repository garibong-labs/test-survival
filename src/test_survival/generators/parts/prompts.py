from importlib.resources import files
import re

CONSTRAINT='Use only classes, constructors and methods that appear in the class source above or in the dependency signatures below. Do not use APIs from other versions of this library. Construct real objects through the constructors or factory methods shown instead of mocking; do not mock or subclass abstract classes or interfaces of this library.'
MODIFIERS=r'public|protected|private|abstract|static|final|strictfp|sealed|non-sealed'


def render(technique,name,source,block='',constraint=False):
    root=files('test_survival.generators.parts').joinpath('templates')
    template=root.joinpath(technique+'.txt').read_text()
    appended=('\n\n'+CONSTRAINT if constraint else '')+('\n\n'+block if block else '')
    values={'{class_name}':name,'{source_code}':source+appended,'{fewshot_example}':root.joinpath('fewshot_example.txt').read_text()}
    return ''.join(values.get(piece,piece) for piece in re.split(r'(\{class_name\}|\{source_code\}|\{fewshot_example\})',template))


def gate(block):
    if re.search(r'<\s*>',block):
        raise ValueError('Dependency block has empty type parameters')
    for line in block.splitlines():
        if not line.strip() or line.startswith((' ', '//')) or line == '}':
            continue
        # Inventory headers are normalized to one line; Java's default access
        # has no modifier. Still require a declaration kind, name, and body.
        match=re.fullmatch(r'(?:(?:'+MODIFIERS+r')\s+)*(class|interface|enum|record|@interface)\s+'
                           r'([A-Za-z_$][\w$]*)(.*?)\s*\{',line)
        if not match:
            raise ValueError('Malformed dependency block type header')
        kind,_,tail=match.groups()
        tail=tail.strip()
        def consume_group(text,opening,closing):
            depth=0
            for index,char in enumerate(text):
                if char==opening:depth+=1
                elif char==closing:
                    depth-=1
                    if depth==0:return text[index+1:].strip()
            raise ValueError('Malformed dependency block type header')
        if tail.startswith('<'):tail=consume_group(tail,'<','>')
        if kind=='record':
            if not tail.startswith('('):raise ValueError('Malformed dependency block record header')
            tail=consume_group(tail,'(',')')
        if tail and not re.fullmatch(r'(?:(?:extends|implements|permits)\s+[\w$.,<>?@\[\] &]+\s*)+',tail):
            raise ValueError('Malformed dependency block type header')
