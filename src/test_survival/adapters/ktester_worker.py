"""Isolated KTester preparation and invocation; author code remains in tool_path."""
import argparse
import collections
import csv
import hashlib
import json
import logging
import itertools
import threading
import subprocess
import os
from pathlib import Path
import re
import shutil
import sys
import time
import types
from typing import NamedTuple
sys.dont_write_bytecode=True
if __package__:
    from . import java_worker, ktester_classpath
    from ..rules.ktester_round import fully_visited
    from ..rules.matching import renumber
else:  # Direct worker invocation inside the KTester container.
    import java_worker, ktester_classpath
    from rules.ktester_round import fully_visited
    from rules.matching import renumber
execute, write_json = java_worker.execute, java_worker.write_json


def csvout(path,rows,fields):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore',lineterminator='\n');writer.writeheader();writer.writerows(rows)

def start_jvm():
    import jpype
    jpype.startJVM(jpype.getDefaultJVMPath(),'-Xmx4g',classpath=[str(TOOL/'code/Java/project-info-process.jar'),str(TOOL/'code/Java/project-index-builder.jar')])


def prepare():
    PROJECT.mkdir(parents=True,exist_ok=True)
    for source,dest in [('src/main','src/main'),('src/test/java','src/test-original/java'),*[('src/test/'+n,'src/test/'+n) for n in java_worker.frozen_test_dirs(REPO/'src/test')],('target/classes','target/classes')]:
        if (REPO/source).exists():shutil.copytree(REPO/source,PROJECT/dest,dirs_exist_ok=True)
    shutil.copy2(REPO/'pom.xml',PROJECT/'pom.xml')
    attempts=[]
    submodule=java_worker.maven_dependencies.reactor(REPO,JOB.get('module','.'))[1]!=[]
    reactor_libs=REPO/'target/survival-ktester-libs'
    for attempt in (1,2):
        if attempt==2:time.sleep(60)
        offline=['-o'] if attempt==1 else []
        if submodule:
            # Sibling SNAPSHOTs resolve only in the checkout's own reactor; the copied pom alone cannot find them.
            # A relative directory is per module, so upstream modules' own dependencies stay out of KTester's libs.
            if reactor_libs.exists():shutil.rmtree(reactor_libs)
            args,cwd=java_worker.maven_dependencies.command('dependency:copy-dependencies',REPO,JOB['module'],offline=attempt==1,
                                                            options=('-DoutputDirectory='+str(reactor_libs.relative_to(REPO)),'-DincludeScope=test'))
        else:
            args,cwd=['mvn','-q',*offline,'dependency:copy-dependencies','-DoutputDirectory=libs','-DincludeScope=test'],PROJECT
        run=execute(args,cwd,RESULTS/f'ktester-prepare-{attempt}.txt')
        attempts.append(run)
        if run['exit_code']==0:break
    assert attempts[-1]['exit_code']==0,attempts
    if submodule:
        if (PROJECT/'libs').exists():shutil.rmtree(PROJECT/'libs')
        if reactor_libs.is_dir():shutil.copytree(reactor_libs,PROJECT/'libs')
    (PROJECT/'libs').mkdir(exist_ok=True)
    jars=sorted((PROJECT/'libs').glob('*.jar'))
    (PROJECT/'dependencies.txt').write_text('target/classes:'+':'.join('libs/'+p.name for p in jars))
    write_json(DS/'dataset_info.json',{PROJECT_NAME:{'project-name':PROJECT_NAME,'project-url':'projects/'+PROJECT_NAME,'focal-methods':[]}})
    (DS/'.gitignore').write_text('**/target/\n**/*.jar\n__pycache__/\n')
    write_json(RESULTS/'ktester-snapshot.json',dict(attempts=attempts,jars_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in jars},jars_added_beyond_maven=0))


def index():
    import jpype
    start=time.monotonic()
    for name in ['json','codegraph','lucene']:(INDEX/name).mkdir(parents=True,exist_ok=True)
    start_jvm()
    jpype.JClass('PreProcessor').main([str(DS/'projects'),str(INDEX)])
    sys.path.insert(0,str(TOOL/'code'))
    import procedure.preprocess_project as pp
    fs=types.SimpleNamespace(DATASET_PATH=str(DS),CODE_INFO_PATH=str(INDEX))
    pp.build_calling_graph(fs);pp.extract_invoke_patterns(fs)
    jpype.JClass('IndexBuilder').main(['group',str(INDEX/'json'),str(INDEX/'lucene')])
    info=json.loads((INDEX/'json'/(PROJECT_NAME+'.json')).read_text())
    counts={part:dict(classes=len(info[part]),methods=sum(len(entries) for cls in info[part].values() for entries in cls['methods'].values()),constructors=sum(len(cls['constructors']) for cls in info[part].values())) for part in ['source','test']}
    result=dict(status='completed',seconds=round(time.monotonic()-start,3),UNREACHABLE_TARGETS=pp.UNREACHABLE_TARGETS,counts=counts)
    write_json(RESULTS/'ktester-index.json',result);print(json.dumps(result),flush=True)
    jpype.shutdownJVM()


def erase_generics(signature):
    while re.search(r'<[^<>]*>', signature):
        signature = re.sub(r'<[^<>]*>', '', signature)
    return signature


# Only unquoted parentheses outside comments affect the annotation argument depth.
ANNOTATION_NAME = re.compile(r'@[\w$]+(?:\s*\.\s*[\w$]+)*\s*')
ARGUMENT_TOKEN = re.compile(
    r'''/\*.*?\*/|//[^\r\n]*|"""(?:\\.|(?!""").)*"""|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[()]''', re.DOTALL)


def without_annotations(signature):
    if '@' not in signature:
        return signature
    parts = []
    start = 0
    while match := ANNOTATION_NAME.search(signature, start):
        parts.append(signature[start:match.start()])
        start = match.end()
        if signature.startswith('(', start):
            depth = 1
            for token in ARGUMENT_TOKEN.finditer(signature, start + 1):
                if token[0] == '(':
                    depth += 1
                elif token[0] == ')':
                    depth -= 1
                    if depth == 0:
                        start = token.end()
                        break
            while start < len(signature) and signature[start].isspace():
                start += 1
    parts.append(signature[start:])
    return re.sub(r'\s*\.\s*', '.', ''.join(parts))


def simple_key(signature):
    simple = re.sub(r'(?:\w+\.)+(\w+)', r'\1', erase_generics(without_annotations(signature)))
    return re.sub(r'\s+', '', simple.replace('...', '[]'))


def matching_positions(signature, qualified):
    params = erase_generics(without_annotations(signature)).partition('(')[2][:-1]
    params = re.sub(r'\s+', '', params).replace('...', '[]')
    types = params.split(',') if params else []
    return [pos for pos, full_types in qualified.items() if len(types) == len(full_types)
            and all(full == t or full.endswith('.' + t) for t, full in zip(types, full_types))]


class FocalMethod(NamedTuple):
    cls: str
    original: str
    lookup: str


def lookup_spelling(extractor, ct, method, original):
    name = str(method.getSimpleName())
    simple = ', '.join(str(p.getType().getSimpleName()) for p in method.getParameters())
    for spelling in dict.fromkeys([original, without_annotations(original), f'{name}({simple})']):
        # DatasetPrepare splits on every opening parenthesis. Annotation arguments
        # would truncate method-name, and can leave its generic-erasure loop unbalanced.
        if spelling.count('(') != 1:
            continue
        args = erase_generics(spelling).partition('(')[2][:-1]
        if extractor.getCtMethod(ct, name, args.split(', ') if args else []) == method:
            return spelling
    return None


def match_class_methods(cls, eligible, extractor, index_methods, errors):
    inventory = collections.defaultdict(list)
    for signature in eligible:
        inventory[simple_key(signature)].append(signature)

    spoon = collections.defaultdict(list)
    ct = extractor.getCtClass(cls)
    ordered = sorted(ct.getMethods(), key=lambda m: m.getPosition().getLine()) if ct is not None else []
    for order, method in enumerate(ordered):
        params = [p.getType() for p in method.getParameters()]
        simple = ', '.join(str(t.getSimpleName()) for t in params)
        key = simple_key(f'{method.getSimpleName()}({simple})')
        if key in inventory:
            spoon[key].append((order, params))

    indexed = collections.defaultdict(list)
    for name in {key.partition('(')[0] for key in inventory}:
        for entry in index_methods.get(name, []):
            declaration = entry['signature']
            signature = declaration[declaration.index(name + '('):]
            indexed[simple_key(signature)].append(signature)

    selected = {}
    for key, signatures in inventory.items():
        name = key.partition('(')[0]
        overloads, candidates = spoon[key], indexed[key]
        if len(signatures) == len(overloads) == len(candidates) == 1:
            selected[overloads[0][0]] = candidates[0]
            continue

        # Spoon's qualified name retains the enclosing chain as pkg.Outer$Inner$X.
        qualified = {pos: tuple(str(t.getQualifiedName()).replace('$', '.') for t in params)
                     for pos, params in overloads}
        index_positions = [(s, matching_positions(s, qualified)) for s in candidates]
        for signature in signatures:
            positions = matching_positions(signature, qualified)
            if len(positions) == 1:
                pos = positions[0]
                entries = [(s, matches) for s, matches in index_positions if pos in matches]
                if pos not in selected and len(entries) == 1 and entries[0][1] == positions:
                    selected[pos] = entries[0][0]
                    continue
            spoon_names = [name + '(' + ', '.join(types) + ')' for types in qualified.values()]
            errors.append(f'{cls}: inventory={signature}, Spoon={spoon_names}, index={candidates}')

    matches = []
    for pos, original in sorted(selected.items()):
        lookup = lookup_spelling(extractor, ct, ordered[pos], original)
        if lookup is None:
            errors.append(f'{cls}.{original}: DatasetPrepare lookup did not resolve the expected Spoon method')
        else:
            matches.append(FocalMethod(cls, original, lookup))
    return matches


def match_methods(classes, eligible, extractor, indexed):
    matches = []
    errors = []
    for cls in classes:
        matches.extend(match_class_methods(cls, eligible.get(cls, []), extractor,
                                           indexed.get(cls, {}).get('methods', {}), errors))
    if errors:
        raise ValueError('Cannot match KTester overloads one to one:\n' + '\n'.join(errors))
    return matches


def restore_method_names(entries, matches):
    originals = {(m.cls, m.lookup): m.original for m in matches}
    empty = []
    for row in entries:
        key = row['class'], row['method-name']
        row['method-name'] = originals[key]
        if not (row.get('focal-method') or '').strip():
            empty.append(row['class'] + '.' + row['method-name'])
    if empty:
        raise ValueError('Empty KTester focal-method bodies:\n' + '\n'.join(empty))


def validate_population(entries, methods):
    # The author writes test_classes/<simple test class>.java, then we archive those flat files.
    owners = {field: {} for field in ('id', 'test-class', 'test-path', 'filename')}
    actual = []
    for row in entries:
        method = row['class'] + '.' + row['method-name']
        actual.append(method)
        filename = row['test-class'].rsplit('.', 1)[-1] + '.java'
        for field, seen in owners.items():
            value = filename if field == 'filename' else row[field]
            if value in seen:
                raise ValueError(f'KTester {field} {value} is shared by {seen[value]} and {method}')
            seen[value] = method
    prepared, expected = collections.Counter(actual), collections.Counter(methods)
    if prepared != expected:
        raise ValueError(f'KTester population differs from matched methods: '
                         f'missing={expected - prepared}, extra={prepared - expected}')
    return dict(valid=True, entries=len(entries), unique_ids=len(owners['id']))


def dataset():
    import jpype
    inventory=json.loads((RESULTS/'source-inventory.json').read_text())
    eligible = collections.defaultdict(list)
    for method in inventory['main']['methods']:
        if method['eligible']:
            eligible[method['class']].append(method['signature'])
    scope=json.loads((EXP/'population/scope.json').read_text())
    start_jvm()
    extractor=jpype.JClass('extractor.SpoonExtractor')([str(PROJECT/'src/main/java')],[str(PROJECT/'libs')])
    indexed=json.loads((INDEX/'json'/(PROJECT_NAME+'.json')).read_text())['source']
    matches = match_methods([c['class'] for c in scope['classes']], eligible, extractor, indexed)
    assert len(matches) == scope['m'], (len(matches), scope['m'])
    methods = [m.cls + '.' + m.original for m in matches]
    lookups = {m.cls + '.' + m.lookup: i for i, m in enumerate(matches)}
    meta = [dict(project_name=PROJECT_NAME, put_path='projects/'+PROJECT_NAME, method_name_to_idx=lookups)]
    write_json(DS/'dataset_meta.json', meta)
    jpype.JClass('DatasetPrepare').main([str(DS)])
    jpype.shutdownJVM()
    shutil.copy2(DS/'dataset_info.json',DS/'dataset_info.raw.json')
    data=json.loads((DS/'dataset_info.raw.json').read_text())
    restore_method_names(data[PROJECT_NAME]['focal-methods'], matches)
    entries=renumber(data[PROJECT_NAME]['focal-methods'])
    checked=validate_population(entries,methods)
    data[PROJECT_NAME]['focal-methods']=entries
    write_json(DS/'dataset_info.json',data)
    substitutions = [{'class': m.cls, 'original': m.original, 'lookup': m.lookup}
                     for m in matches if m.original != m.lookup]
    if substitutions:
        meta[0]['method_name_to_idx'] = {m: i for i, m in enumerate(methods)}
        write_json(DS/'dataset_meta.json', meta)
    for name in ['dataset_info.raw.json','dataset_info.json','dataset_meta.json']:
        shutil.copy2(DS/name,EXP/'population'/name)
    csvout(EXP/'population/ktester-population.csv',entries,['id','class','method-name','test-class','test-path'])
    csvout(EXP/'population/ktester-lookup-spellings.csv', substitutions, ['class','original','lookup'])
    write_json(RESULTS/'ktester-population-validation.json',checked)
    print(json.dumps(checked),flush=True)


class PostProcess(logging.Handler):
    """Observe terminal outcomes for dataset IDs and the author's project future."""
    def __init__(self):
        super().__init__()
        self.started = set()
        self.outcomes = {}
        self.finished = False
        self.project_error = None

    def emit(self, record):
        message = record.getMessage()
        if message.startswith('Post process completed:'):
            self.finished = True
        elif message.startswith('Error in post process:'):
            self.project_error = message

    def summary(self, expected, failure=None):
        visited = expected.intersection(self.started)
        errors = [error for i, error in self.outcomes.items() if i in expected and error]
        error = self.project_error
        if not expected.issubset(self.outcomes) or not self.finished:
            error = error or failure or f'Project post-process ended after {len(visited)} of {len(expected)} classes'
        return dict(status='incomplete' if error else 'completed', visited=len(visited),
                    total=len(expected), skipped=len(errors), error=error or next(iter(errors), None))


def observe_repairs(author, out, observer=None):
    repairer = getattr(getattr(author, 'Post', None), 'CodeRepairer', None)
    original_check = getattr(repairer, 'check_test_class', None)
    if not callable(original_check):
        return

    def marker(task, status, error=None):
        try:
            write_json(out/'progress'/(task['id']+'.json'),
                       {'class': task['test-class'], 'status': status, 'error': error})
        except OSError:
            pass  # Live progress is optional. The final count uses in-memory outcomes.

    def check(self, task, *args, **kwargs):
        if observer is not None:
            observer.started.add(task['id'])
        marker(task, 'running')
        try:
            value = original_check(self, task, *args, **kwargs)
        except Exception as exc:
            error = f"{task['test-class']}: {type(exc).__name__}: {exc}"
            if observer is not None:
                observer.outcomes[task['id']] = error
            marker(task, 'failed', type(exc).__name__)
            raise
        if observer is not None:
            observer.outcomes[task['id']] = None
        marker(task, 'completed')
        return value

    repairer.check_test_class = check


def resolve_console(version, destination):
    # Projects can bring any JUnit release. Fetch just its matching launcher
    # when the runner cache does not already have it.
    coordinate = f'org.junit.platform:junit-platform-console-standalone:{version}'
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(dir=destination.parent) as scratch:
        staged = Path(scratch)/destination.name
        java_worker.maven_dependencies.maven(
            'dependency:copy', Path(scratch), RESULTS/'ktester-console', 600, execute,
            options=('-Dartifact='+coordinate, '-DoutputDirectory='+scratch), verify=staged.is_file)
        staged.replace(destination)


def retire_round(out, destination):
    from uuid import uuid4
    history = EXP/'work/generation-history'/uuid4().hex/JOB['group']
    for path in (destination, out):
        if path.exists():
            record = java_worker.result_io.read_json(path/'run.json', {})
            verified_copy = fully_visited(record) and (path == destination or not destination.exists())
            if record.get('status') != 'completed' or verified_copy:
                raise RuntimeError('Interrupted or unexpected KTester round; inspect '+str(path))
    for path, kind in ((out, 'worker'), (destination, 'generated')):
        if path.exists():
            archived = history/kind/path.name
            archived.parent.mkdir(parents=True)
            path.rename(archived)
    # These two directories contain only the author's generated sources and
    # compiled tests. Keep an old skipped class out of the replacement snapshot.
    for relative in ('src/test/java', 'target/test-classes'):
        path = PROJECT/relative
        if path.exists():
            archived = history/'project-workspace'/relative
            archived.parent.mkdir(parents=True, exist_ok=True)
            path.rename(archived)


def generate():
    args=types.SimpleNamespace(round=JOB['round'],attempt=JOB['retry']+1)
    label=JOB['label']
    ds=DS
    out=ds/'rounds'/label
    destination = EXP/'generated'/JOB['group']/label
    os.chdir(TOOL/'code');sys.path.insert(0,str(TOOL/'code'))
    from settings import FileStructure as FS, TaskSettings as TS, LLMSettings as LS, TimeRecord as TR
    FS.DATASET_PATH=os.path.relpath(DS,TOOL/'code')
    # Resolve dependencies before opening the model-call ledger.
    supplement = PROJECT/'target/ktester-test-libs'
    classpath = ktester_classpath.configure(PROJECT, EXP/'work/libs', supplement, resolve_console)
    if out.exists() or destination.exists():
        retire_round(out, destination)
    out.mkdir(parents=True)
    write_json(out/'test-classpath.json', classpath)
    FS.DEPENDENCY_PATH = os.path.relpath(supplement, TOOL/'code')
    FS.CODE_INFO_PATH=FS.DATASET_PATH+'/project_index'
    for field,folder in [('PROMPT_PATH','context+prompts'),('FIX_PATH','fix'),('RESPONSE_PATH','responses'),('TESTCLASSS_PATH','test_classes'),('REPORT_PATH','reports')]:
        setattr(FS,field,str(out/'<project>'/folder))
    TR.TIME_RECORD_PATH=str(out/'time')
    TS.PROJECTS=[PROJECT_NAME]
    TS.FIX_TRIES=JOB['params']['fix_tries'];TS.MAX_WORKERS=JOB['params']['workers'];LS.TEMPERATURE=JOB['params']['temperature'];LS.MODEL=JOB['model']
    key=os.environ[JOB['env_key']]
    LS.API_ACCOUNTS=[dict(base_url='https://openrouter.ai/api/v1',api_key=key)]
    # Capture every API request/response, excluding all authorization headers.
    import openai
    from openai.resources.chat.completions import Completions
    from tenacity import wait_fixed,stop_after_attempt
    original_init=openai.OpenAI.__init__
    def initialize(self,*a,**kw):
        kw['max_retries']=0;kw['timeout']=600
        return original_init(self,*a,**kw)
    openai.OpenAI.__init__=initialize
    original_create=Completions.create
    counter=itertools.count(1);lock=threading.Lock()
    raw=out/'raw-responses';raw.mkdir()
    def create(self,*a,**kw):
        with lock:number=next(counter)
        call=raw/f'{number:04d}';call.mkdir()
        body={k:v for k,v in kw.items() if k in ['model','messages','temperature','max_tokens','response_format'] and not isinstance(v,(openai.Omit,openai.NotGiven))}
        write_json(call/'request.json',body)
        start=time.monotonic()
        try:
            response=original_create(self,*a,**kw)
            data=response.model_dump_json()
            if key in data:raise RuntimeError('Credential echoed by server; response not saved')
            (call/'response.json').write_text(data+'\n')
            write_json(call/'call.json',dict(status='ok',seconds=round(time.monotonic()-start,3)))
            return response
        except Exception as exc:
            write_json(call/'call.json',dict(status='error',error_type=type(exc).__name__,seconds=round(time.monotonic()-start,3)))
            raise
    Completions.create=create
    from tools.llm_api import LLMCaller
    LLMCaller._generation=LLMCaller._generation.retry_with(wait=wait_fixed(60),stop=stop_after_attempt(2))
    original_run=subprocess.run
    def bounded(command,*a,**kw):
        if isinstance(command,list) and command:
            binary=Path(str(command[0])).name
            limit={'java':120,'javac':300,'mvn':600}.get(binary)
            if limit:kw['timeout']=min(kw.get('timeout') or limit,limit)
        return original_run(command,*a,**kw)
    subprocess.run=bounded
    logging.basicConfig(filename=str(out/'generation.log'),level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    class SecretFilter(logging.Filter):
        def filter(self,record):
            record.msg=record.getMessage().replace(key,'KEY_OK (value never logged)');record.args=()
            if record.exc_info:
                record.exc_text = logging.Formatter().formatException(record.exc_info).replace(key, 'KEY_REDACTED')
            return True
    for handler in logging.getLogger().handlers:handler.addFilter(SecretFilter())
    import jpype
    import preparation
    preparation.set_file_structure()
    jpype.startJVM(jpype.getDefaultJVMPath(),'-Xmx4g',classpath=['./Java/project-info-process.jar','./Java/project-index-builder.jar'])
    start=time.monotonic();status='completed';error=None
    rows = json.loads((DS/'dataset_info.json').read_text())[PROJECT_NAME]['focal-methods']
    expected = {row['id'] for row in rows}
    observer = PostProcess()
    logger = logging.getLogger('procedure.post_process')
    logger.addHandler(observer)
    try:
        import generate_unit_test
        observe_repairs(generate_unit_test, out, observer)
        generate_unit_test.run()
    except Exception as exc:
        status='failed';error=type(exc).__name__
        logging.error('KTester round failed: %s',type(exc).__name__)
    finally:
        logger.removeHandler(observer)
        jpype.shutdownJVM()
        post = observer.summary(expected, error)
        if post['error']:
            post['error'] = post['error'].replace(key, 'KEY_REDACTED')
        if post['status'] != 'completed':
            status = 'failed'
        result=dict(post_process=post,round=args.round,status=status,error_type=error,seconds=round(time.monotonic()-start,3),model_calls=len(list(raw.glob('*/call.json'))),model=LS.MODEL,temperature=LS.TEMPERATURE,fix_tries=TS.FIX_TRIES,workers=TS.MAX_WORKERS,prompts_unmodified=True,provider_pinned=False)
        write_json(out/'run.json',result)
        if status == 'failed':
            write_json(out/'funnel.json', dict(post_process=post))
        # Never archive credentials, jars, or compiled classes.
        shutil.copytree(out,destination,ignore=shutil.ignore_patterns('*.class','*.jar'),dirs_exist_ok=True)
        project=PROJECT
        if (project/'src/test/java').exists():shutil.copytree(project/'src/test/java',destination/'sources',dirs_exist_ok=True)
        for p in destination.rglob('*'):
            if p.is_file() and key.encode() in p.read_bytes():
                p.write_bytes(p.read_bytes().replace(key.encode(),b'KEY_OK (value never logged)'))
                result['credential_redactions']=result.get('credential_redactions',0)+1
        write_json(RESULTS/f"{JOB['group']}-{label}.json",result)
        print(json.dumps(result),flush=True)
    if status != 'completed':
        raise RuntimeError(error or post['error'])


def main():
    global JOB,EXP,RESULTS,REPO,TOOL,DS,PROJECT,INDEX,PROJECT_NAME
    JOB=json.loads(Path(sys.argv[1]).read_text())
    EXP=Path(JOB['experiment']);RESULTS=EXP/'results';REPO=EXP/'work/checkouts/0'/JOB['module'];TOOL=Path(JOB['tool'])
    PROJECT_NAME=JOB['project_name'];DS=EXP/'work/ktester'/JOB['group'];PROJECT=DS/'projects'/PROJECT_NAME;INDEX=DS/'project_index'
    java_worker.EXP=EXP
    # Supply the prescribed settings in memory; never import a credential-bearing
    # author settings file, and never change any file in the author-tool checkout.
    settings=types.ModuleType('settings')
    settings.FileStructure=type('FileStructure',(),dict(DEPENDENCY_PATH='./dependencies',DATASET_PATH=str(DS),CODE_INFO_PATH=str(INDEX),BASELINE_PATH=str(EXP/'work/ktester-baseline'),PROMPT_PATH='',FIX_PATH='',RESPONSE_PATH='',TESTCLASSS_PATH='',REPORT_PATH=''))
    settings.LLMSettings=type('LLMSettings',(),dict(MODEL=JOB['model'],TEMPERATURE=JOB['params']['temperature'],API_ACCOUNTS=[]))
    settings.TaskSettings=type('TaskSettings',(),dict(PROJECTS=[PROJECT_NAME],CASES_LIST=[],PROMPT_LIST=['condition','io','exception'],CASE_THEN_CODE=True,SAVE_INTER_RESULT=True,COMPILE_TEST=True,REPETITION_NUM=5,MAX_WORKERS=JOB['params']['workers'],FIX_TRIES=JOB['params']['fix_tries'],SIM_TOP_K='10'))
    settings.TimeRecord=type('TimeRecord',(),dict(TIME_RECORD_PATH=str(DS/'time'),TIME_FILE_NAME='time_records.json',START_RECORD=True))
    settings.BaseLine=type('BaseLine',(),dict(BASELINES=[],BASELINE_PATH=str(EXP/'work/ktester-baseline'),UTGEN_DATA=str(EXP/'work/utgen'),CHATUNITEST_DATA=str(EXP/'work/chatunitest')))
    sys.modules['settings']=settings
    if JOB['action']=='retire':
        for label in JOB['superseded']:
            retire_round(DS/'rounds'/label, EXP/'generated'/JOB['group']/label)
    elif JOB['action']=='prepare':prepare()
    elif JOB['action']=='index':index()
    elif JOB['action']=='dataset':dataset()
    elif JOB['action']=='generate':generate()


if __name__=='__main__':main()
