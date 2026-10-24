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
        if re.search(r'\b(class|interface|enum)\s+\w+',line) and not line.startswith((' ','//')):
            if not re.match(r'^(?:(?:'+MODIFIERS+r')\s+)+(?:class|interface|enum)\s+',line):
                raise ValueError('Dependency block type header lacks declaration modifiers')
