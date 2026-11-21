"""Offline fixture entry point inside the KTester image. Run via test_ktester_post_process.py."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from openai.resources.chat.completions import Completions
from openai.types.chat import ChatCompletion

from test_survival.adapters import ktester_worker as worker

root = Path('/fixture')
dataset = root/'work/ktester/k'
project = dataset/'projects/sample'
package = project/'src/main/java/p'
methods = ['skip()', 'nested(Ast.Type)', 'annotated(Ast.@Mark Type)', 'qualified(java.lang.String)']


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def stub(self, *, messages, **kwargs):
    prompt = messages[-1]['content']
    if isinstance(kwargs.get('response_format'), dict):
        content = json.dumps([dict(group='fixture', cases=[dict(input=[], expected='one', description='call')])])
    else:
        repairing = 'Corresponding execution feedback' in prompt
        rows = json.loads((dataset/'dataset_info.json').read_text())['sample']['focal-methods']
        row, = [r for r in rows if (r['test-class'].split('.')[-1] in prompt if repairing
                                    else r['method-name'] in prompt.split('```Java')[0])]
        name = row['test-class'].split('.')[-1]
        if '@input{test class template}' in prompt:
            # Compile the imports and extension copied verbatim from the author's rendered template.
            content = prompt.split('@input{test class template}')[1].split('```java')[1].split('```')[0]
            content = re.sub(r'class \w+ \{', 'class '+name+' {', content)
        else:
            method = row['method-name'].split('(')[0]
            argument = '' if method == 'skip' else 'null'
            content = f'''package p;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.*;
import static org.junit.jupiter.api.Assertions.*;
@ExtendWith(MockitoExtension.class)
public class {name} {{
    @ParameterizedTest @ValueSource(ints={{1}})
    public void exercise(int one, @Mock Runnable mock) {{
        assertNotNull(Mockito.mock(Sample.class));
        mock.run();
        org.mockito.Mockito.verify(mock).run();
        assertEquals({1 if repairing else 0}, new Sample().{method}({argument}));
    }}
}}'''
        content = '```java\n'+content+'\n```'
    return ChatCompletion(id='fixture', model='stub', object='chat.completion', created=0,
                          choices=[dict(index=0, finish_reason='stop', message=dict(role='assistant', content=content))])


def inject(author, out, observer):
    install(author, out, observer)
    verify = author.Post.verify_test_classes

    def verify_fixture(fs, ts, data):
        rows = data['sample']['focal-methods']
        if sys.argv[-1].endswith('truncated.json'):
            ts.CASES_LIST = [rows[0]['id']]
        else:
            # javac flags the duplicate inner class, then Spoon rejects duplicate top-level MultiIfc.
            path = out/'sample/test_classes'/Path(rows[0]['test-path']).name
            path.write_text('package p; class '+path.stem+' {class D {} class D {}}\n'
                            'interface MultiIfc {} interface MultiIfc {}')
        return verify(fs, ts, data)

    author.Post.verify_test_classes = verify_fixture


if __name__ == '__main__' and len(sys.argv) == 2:
    install = worker.observe_repairs
    worker.observe_repairs = inject
    Completions.create = stub
    worker.main()
elif __name__ == '__main__':
    import jpype
    jpype.startJVM(classpath=['/author/code/Java/project-info-process.jar'])
    sterilize = jpype.JClass('editcode.TestClassSterilizer')
    helpers = """class T {
        class Helper { int lookup(java.lang.String s) {return 1;} }
        class Helper { int lookup(java.lang.String s) {return 12345;} int lookup(int n) {return n;} }
    }"""
    merged = str(sterilize.main([helpers, 'inner_class']))
    assert merged.count('class Helper') == 1 and merged.count('int lookup(') == 2 and 'return 12345;' in merged
    jpype.shutdownJVM()
    package.mkdir(parents=True)
    shutil.copytree(root/'work/libs/assertj', project/'libs')
    (project/'src/test-original/java').mkdir(parents=True)
    (package/'Sample.java').write_text('''package p;
import java.lang.annotation.*;
@Target(ElementType.TYPE_USE) @interface Mark {}
class Ast {static class Type {}}
public class Sample {
    public int skip() {return 1;}
    public int nested(Ast.Type t) {return 1;}
    public int annotated(Ast.@Mark Type t) {return 1;}
    public int qualified(java.lang.String s) {return 1;}
}
''')
    subprocess.run(['javac', '-d', project/'target/classes', package/'Sample.java'], check=True)
    job = dict(experiment=str(root), module='.', tool='/author', project_name='sample', group='k',
               round=1, retry=0, label='round1', params=dict(temperature=0, fix_tries=1, workers=1),
               model='stub', env_key='FIXTURE_KEY')
    save(dataset/'dataset_info.json', {'sample': {'project-name':'sample', 'project-url':'projects/sample', 'focal-methods':[]}})
    save(root/'results/source-inventory.json', {'main': {'methods': [
        {'class':'p.Sample', 'eligible':True, 'signature':m} for m in methods]}})
    save(root/'population/scope.json', {'m':len(methods), 'classes':[{'class':'p.Sample'}]})
    for action in ('index', 'dataset', 'generate'):
        save(root/'job.json', {**job, 'action':action})
        subprocess.run([sys.executable, __file__, root/'job.json'], check=True)
    save(root/'truncated.json', {**job, 'action':'generate', 'round':2, 'label':'round2'})
    assert subprocess.run([sys.executable, __file__, root/'truncated.json']).returncode != 0
