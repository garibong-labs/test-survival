"""Isolated KTester preparation and invocation; author code remains in tool_path."""
import argparse
import collections
import csv
import hashlib
import importlib.util
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
sys.dont_write_bytecode=True
from java_worker import execute,write_json
import java_worker


def csvout(path,rows,fields):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore',lineterminator='\n');writer.writeheader();writer.writerows(rows)

def start_jvm():
    import jpype
    jpype.startJVM(jpype.getDefaultJVMPath(),'-Xmx4g',classpath=[str(TOOL/'code/Java/project-info-process.jar'),str(TOOL/'code/Java/project-index-builder.jar')])


def prepare():
    PROJECT.mkdir(parents=True,exist_ok=True)
    for source,dest in [('src/main','src/main'),('src/test/java','src/test-original/java'),('src/test/resources','src/test/resources'),('src/test/suite','src/test/suite'),('target/classes','target/classes')]:
        if (REPO/source).exists():shutil.copytree(REPO/source,PROJECT/dest,dirs_exist_ok=True)
    shutil.copy2(REPO/'pom.xml',PROJECT/'pom.xml')
    attempts=[]
    for attempt in (1,2):
        if attempt==2:time.sleep(60)
        run=execute(['mvn','-q',*(['-o'] if attempt==1 else []),'dependency:copy-dependencies','-DoutputDirectory=libs','-DincludeScope=test'],PROJECT,RESULTS/f'ktester-prepare-{attempt}.txt')
        attempts.append(run)
        if run['exit_code']==0:break
    assert attempts[-1]['exit_code']==0,attempts
    jars=sorted((PROJECT/'libs').glob('*.jar'))
    assert jars
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


def dataset():
    import jpype
    from renumber_dataset_ids import renumber,validate
    inventory=json.loads((RESULTS/'source-inventory.json').read_text())
    def normalized(signature):
        while '<' in signature:
            signature=re.sub(r'<[^<>]*>','',signature)
        return re.sub(r'\s+','',re.sub(r'(?:\w+\.)+(\w+)',r'\1',signature).replace('...','[]'))
    eligible={(m['class'],normalized(m['signature'])) for m in inventory['main']['methods'] if m['eligible']}
    scope=json.loads((EXP/'population/scope.json').read_text())
    start_jvm()
    extractor=jpype.JClass('extractor.SpoonExtractor')([str(PROJECT/'src/main/java')],[str(PROJECT/'libs')])
    indexed=json.loads((INDEX/'json'/(PROJECT_NAME+'.json')).read_text())['source']
    methods=[]
    for c in scope['classes']:
        ct=extractor.getCtClass(c['class'])
        for m in sorted(ct.getMethods(),key=lambda x:x.getPosition().getLine()):
            sig=str(m.getSimpleName())+'('+', '.join(str(p.getType().getSimpleName()) for p in m.getParameters())+')'
            if (c['class'],normalized(sig)) in eligible:
                name=str(m.getSimpleName())
                candidates=[name+x['signature'].split(name,1)[1] for x in indexed[c['class']]['methods'][name] if normalized(name+x['signature'].split(name,1)[1])==normalized(sig)]
                assert len(candidates)==1,(sig,candidates)
                methods.append(c['class']+'.'+candidates[0])
    assert len(methods)==scope['m'],(len(methods),scope['m'])
    meta=[dict(project_name=PROJECT_NAME,put_path='projects/'+PROJECT_NAME,method_name_to_idx={m:i for i,m in enumerate(methods)})]
    write_json(DS/'dataset_meta.json',meta)
    jpype.JClass('DatasetPrepare').main([str(DS)])
    jpype.shutdownJVM()
    shutil.copy2(DS/'dataset_info.json',DS/'dataset_info.raw.json')
    raw=json.loads((DS/'dataset_info.raw.json').read_text());data=renumber(raw)
    checked=validate(raw,data,expected_count=scope['m'])
    write_json(DS/'dataset_info.json',data)
    for name in ['dataset_info.raw.json','dataset_info.json','dataset_meta.json']:
        shutil.copy2(DS/name,EXP/'population'/name)
    entries=data[PROJECT_NAME]['focal-methods']
    actual={m['class']+'.'+m['method-name'] for m in entries}
    assert actual==set(methods),(actual^set(methods))
    csvout(EXP/'population/ktester-population.csv',entries,['id','class','method-name','test-class','test-path'])
    write_json(RESULTS/'ktester-population-validation.json',checked)
    print(json.dumps(checked),flush=True)


def observe_repairs(author, out):
    """Best-effort observer: missing hooks or marker failures never skip repair."""
    try:
        repairer = author.Post.CodeRepairer
        original_check = repairer.check_test_class
        if not callable(original_check):
            return

        def marker(task_info, status):
            try:
                path = out/'progress'/(task_info['id']+'.json')
                write_json(path, {'class': task_info['test-class'], 'status': status})
            except Exception:
                pass  # Observability must not change the author's result.

        def check(self, task_info, *args, **kwargs):
            marker(task_info, 'running')
            try:
                value = original_check(self, task_info, *args, **kwargs)
            except BaseException:
                marker(task_info, 'failed')
                raise
            marker(task_info, 'completed')
            return value

        repairer.check_test_class = check
    except Exception:
        pass  # Other author-tool revisions may not expose this optional hook.


def generate():
    args=types.SimpleNamespace(round=JOB['round'],attempt=JOB['retry']+1)
    label=JOB['label']
    ds=DS
    out=ds/'rounds'/label
    if (out/'run.json').exists():return
    if out.exists():raise RuntimeError('Interrupted KTester calls require ledger inspection before retry')
    out.mkdir(parents=True)
    os.chdir(TOOL/'code');sys.path.insert(0,str(TOOL/'code'))
    from settings import FileStructure as FS, TaskSettings as TS, LLMSettings as LS, TimeRecord as TR
    FS.DATASET_PATH=os.path.relpath(DS,TOOL/'code')
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
            record.msg=record.getMessage().replace(key,'KEY_OK (value never logged)');record.args=();return True
    for handler in logging.getLogger().handlers:handler.addFilter(SecretFilter())
    import jpype
    import preparation
    preparation.set_file_structure()
    jpype.startJVM(jpype.getDefaultJVMPath(),'-Xmx4g',classpath=['./Java/project-info-process.jar','./Java/project-index-builder.jar'])
    start=time.monotonic();status='completed';error=None
    try:
        import generate_unit_test
        observe_repairs(generate_unit_test,out)
        generate_unit_test.run()
    except Exception as exc:
        status='failed';error=type(exc).__name__
        logging.error('KTester round failed: %s',type(exc).__name__)
    finally:
        jpype.shutdownJVM()
        result=dict(round=args.round,status=status,error_type=error,seconds=round(time.monotonic()-start,3),model_calls=len(list(raw.glob('*/call.json'))),model=LS.MODEL,temperature=LS.TEMPERATURE,fix_tries=TS.FIX_TRIES,workers=TS.MAX_WORKERS,prompts_unmodified=True,provider_pinned=False)
        write_json(out/'run.json',result)
        destination=EXP/'generated'/JOB['group']/label
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
    spec=importlib.util.spec_from_file_location('matching',Path(__file__).parents[1]/'rules/matching.py');matching=importlib.util.module_from_spec(spec);spec.loader.exec_module(matching)
    def renumber(raw):
        result=json.loads(json.dumps(raw));result[PROJECT_NAME]['focal-methods']=matching.renumber(result[PROJECT_NAME]['focal-methods']);return result
    def validate(raw,data,expected_count):
        rows=data[PROJECT_NAME]['focal-methods']
        if len(rows)!=expected_count or len({r['id'] for r in rows})!=expected_count:raise RuntimeError('KTester dataset validation failed')
        return dict(valid=True,entries=len(rows),unique_ids=len({r['id'] for r in rows}))
    sys.modules['renumber_dataset_ids']=types.SimpleNamespace(renumber=renumber,validate=validate)
    if JOB['action']=='prepare':prepare()
    elif JOB['action']=='index':index()
    elif JOB['action']=='dataset':dataset()
    elif JOB['action']=='generate':generate()


if __name__=='__main__':main()
