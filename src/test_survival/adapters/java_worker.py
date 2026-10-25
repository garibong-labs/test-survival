"""Container-only JUnit worker, ported from run_commit.py.

A complete file/group job runs inside one container. Only the job's new experiment
folder is written. The host orchestrator supplies JSON; this module needs stdlib only.
"""
import collections
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


RUNNER_JAR_RETRIES = 3
RUNNER_JAR_BACKOFF_SECONDS = 60
RUNNER_JAR_TRANSIENT_PATTERNS = ('429', 'Could not transfer')


try:
    from ..rules.environment import matches as environment_matches
    from . import maven_dependencies
except ImportError:  # This worker is also invoked directly inside the container.
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from rules.environment import matches as environment_matches
    from adapters import maven_dependencies


def rerun_environment_failures(results, launch_class, settings, wait=time.sleep):
    pending=[r for r in results if r['state']!='pass' and environment_matches(r.get('retry_message') or r.get('message',''),settings['patterns'])]
    for result in pending:
        result['environment_attempts']=[dict(state=result['state'],message=result.get('retry_message') or result.get('message',''))]
    for attempt in range(1,settings['max_reruns']+1):
        if not pending:break
        wait(settings['backoff_seconds'])
        observations=launch_class(attempt)
        for result in pending:
            state,message=observations[result['id']]
            result.update(state=state,message=message,flaky=False,environment_reruns=attempt)
            result['environment_attempts'].append(dict(state=state,message=message))
        pending=[r for r in pending if r['state']!='pass']


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2)+'\n')
    temporary.replace(path)


def execute(command,cwd,log,timeout=600):
    log.parent.mkdir(parents=True,exist_ok=True)
    start=time.monotonic()
    with log.open('w') as out:
        child=subprocess.Popen(command,cwd=cwd,stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
        timed_out=False
        try: code=child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out=True
            os.killpg(child.pid,signal.SIGKILL)
            code=child.wait()
    return dict(command=command,cwd=str(cwd),exit_code=code,timeout=timed_out,seconds=round(time.monotonic()-start,3),log=str(log.relative_to(EXP)))


def classpath(index):
    records=json.loads((HELPERS/'classpath-artifacts.json').read_text())
    jars=sorted([r for r in records if r['role']=='test'],key=lambda r:r['filename'].startswith('junit-platform-console-standalone'))
    testjars=[str(EXP/'work/libs'/r['filename']) for r in jars]
    folder=EXP/f'work/checkouts/{index}'/MODULE
    build=json.loads((RESULTS/f'builds/{index}.json').read_text())
    cache=EXP/build['classpath']
    maven_dependencies.resolve(folder,cache,RESULTS/'builds/logs'/f'classpath-{index}',TIMEOUTS['build'],execute)
    if any(not Path(p).exists() for p in testjars):
        setup()
    projectjars=[p for p in cache.read_text().strip().split(':') if p and not any(s in p for s in ['/org/junit/','/junit/junit/','/org/opentest4j/','/org/apiguardian/'])]
    return folder,':'.join([str(folder/'target/classes'),*testjars,*projectjars])


def parse_reports(folder):
    observations=collections.defaultdict(list)
    for path in sorted(folder.glob('TEST-*.xml')):
        for case in ET.parse(path).getroot().iter('testcase'):
            output='\n'.join(n.text or '' for n in case.findall('system-out'))
            match=re.search(r'\[(?:method|test-template|test-factory):([^\]]+)\]',output)
            label=match.group(1) if match else case.get('name','')
            name=re.match(r'^([A-Za-z_$][\w$]*)(?:\(|\[|$)',label)
            if not name:continue
            problem=case.find('error')
            if problem is None:problem=case.find('failure')
            kind=problem.get('type','') if problem is not None else ''
            state='absent' if case.find('skipped') is not None else ('fail' if 'Assertion' in kind or kind.endswith(('ComparisonFailure','MultipleFailuresError')) else 'error') if problem is not None else 'pass'
            message=(problem.get('message','') or problem.text or '') if problem is not None else ''
            if kind and kind not in message:message=kind+': '+message
            observations[(case.get('classname'),name.group(1))].append(dict(state=state,message=message[:4000],raw_name=case.get('name')))
    return observations


def method_state(row,observations,run):
    if run['timeout']:return 'timeout','Class run exceeded 120 seconds'
    found=observations.get((row['class'],row['method']),[])
    if not found:return 'absent','Method not reported by launcher'
    for state in ['error','fail','absent']:
        for item in found:
            if item['state']==state:return state,item['message']
    return 'pass',''


def selectors(rows,runtime,folder):
    selected={}
    for cls in sorted({r['class'] for r in rows}):
        log=folder/('selectors-'+cls+'.txt')
        run=execute(['java','-Xmx'+JVM['launcher'],'-cp',runtime,str(HELPERS/'MethodSelectors.java'),cls,TEST_FRAMEWORK],REPO,log,TIMEOUTS['test_class'])
        choices=collections.defaultdict(list)
        if run['exit_code']==0:
            for line in log.read_text().splitlines():
                if '\t' in line:
                    name,selector=line.split('\t',1);choices[name].append(selector)
        for row in rows:
            if row['class']==cls:selected[row['id']]=sorted(set(choices[row['method']]))
    return selected


def jacoco_version(classes):
    for path in classes.rglob('*.class'):
        with path.open('rb') as stream:header=stream.read(8)
        if header[:4]==b'\xca\xfe\xba\xbe' and int.from_bytes(header[6:8],'big')>66:
            return '0.8.14'
    return '0.8.12'


def run_file(index,group,filename,members,part,coverage_file=None,repetitions=1,retry=True,precompiled=None,environment_retry=None):
    folder,cp=classpath(index)
    part.mkdir(parents=True,exist_ok=True)
    classes=part/'classes';classes.mkdir(exist_ok=True)
    source=EXP/filename
    sourcepath=EXP/'dev/src-test-t/java' if DEVELOPER else source.parent
    if precompiled is not None:
        classes,compile_run=precompiled
    else:
        compile_run=execute(['javac','-J-Xmx'+JVM['javac'],'-cp',cp,'-sourcepath',str(sourcepath),'-d',str(classes),str(source)],folder,part/'javac.txt',TIMEOUTS['compile'])
    record=dict(file=filename,compile=compile_run,runs=[],methods=[])
    if compile_run['exit_code']:
        record['methods']=[{**r,'state':'compile-fail','initial_state':'compile-fail','baseline_states':[],'selector':[],'flaky':False,'message':(part/'javac.txt').read_text(errors='replace')[:4000]} for r in members]
        return record
    runtime=':'.join([str(classes),cp,str(EXP/'dev/src-test-t/resources'),str(EXP/'dev/src-test-t/suite')])
    selection=selectors(members,runtime,part)
    base=['java','-Xmx'+JVM['launcher'],'-XX:+UseSerialGC']
    if coverage_file:base+=[f'-javaagent:{SHARED}/work/libs/org.jacoco.agent-{jacoco_version(folder/"target/classes")}-runtime.jar=destfile={coverage_file},append=true']
    base+=['-cp',runtime,'org.junit.platform.console.ConsoleLauncher','--disable-banner','--disable-ansi-colors','--fail-if-no-tests','--details','summary']
    def launch(chosen,label):
        reports=part/label;reports.mkdir(exist_ok=True)
        args=[]
        for r in chosen:
            for selector in selection[r['id']]:args+=['--select-method',selector]
        if not args:
            return dict(command=[],cwd=str(SHARED/'work/runtime'),timeout=False,exit_code=2,seconds=0,log='',selection='no compiled selector'),{}
        run=execute([*base,*args,'--reports-dir',str(reports)],SHARED/'work/runtime',reports/'console.txt',TIMEOUTS['test_class'])
        record['runs'].append(run)
        return run,parse_reports(reports)
    runs=[launch(members,'run'+str(i)) for i in range(1,repetitions+1)]
    for row in members:
        states=[method_state(row,obs,run) for run,obs in runs]
        state,message=next(((s,m) for s,m in states if s!='pass'),('pass',''))
        result={**row,'state':state,'message':message,'flaky':False,'initial_state':state,'selector':selection[row['id']],'baseline_states':[s for s,_ in states]}
        for retry_number in range(FLAKY_RERUN if retry and state in ['fail','error'] and not environment_matches(message,ENVIRONMENT_ERRORS['patterns']) else 0):
            rerun,obs=launch([row],'retry-'+row['method']+'-'+str(retry_number))
            retry_state,retry_message=method_state(row,obs,rerun)
            result.update(retry_state=retry_state,retry_message=retry_message)
            if retry_state=='pass':
                result.update(state='pass',flaky=True)
                break
            if environment_matches(retry_message,ENVIRONMENT_ERRORS['patterns']):
                result.update(state=retry_state,message=retry_message)
                break
        record['methods'].append(result)
    if (retry if environment_retry is None else environment_retry) and ENVIRONMENT_ERRORS['patterns']:
        def launch_class(attempt):
            run,observations=launch(members,'environment-'+str(attempt))
            return {r['id']:method_state(r,observations,run) for r in members}
        rerun_environment_failures(record['methods'],launch_class,ENVIRONMENT_ERRORS)
    return record


def line_rows(xml):
    rows=[]
    for package in ET.parse(xml).getroot().findall('package'):
        for source in package.findall('sourcefile'):
            path='src/main/java/'+package.get('name')+'/'+source.get('name')
            for line in source.findall('line'):
                rows.append(dict(file=path,line=int(line.get('nr')),missed=int(line.get('mi')),covered=int(line.get('ci'))))
    return sorted(rows,key=lambda r:(r['file'],r['line']))


def setup(wait=time.sleep):
    libs=EXP/'work/libs';libs.mkdir(parents=True,exist_ok=True)
    records=json.loads((HELPERS/'classpath-artifacts.json').read_text())
    for r in records:
        dest=libs/r['filename']
        if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest()==r['sha256']:
            continue
        group,artifact,version=r['coordinate'].split(':')
        source=Path('/root/.m2/repository')/group.replace('.','/')/artifact/version/r['filename']
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else None
        if source_sha256 != r['sha256']:
            coordinate=r['coordinate']+(':jar:'+r['classifier'] if r.get('classifier') else '')
            key=group+'.'+Path(r['filename']).stem
            evidence=RESULTS/f'runner-dependency-{key}.json'
            attempts=json.loads(evidence.read_text()).get('attempts',[]) if evidence.exists() else []
            for retry in range(RUNNER_JAR_RETRIES+1):
                cached_sha256=hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else None
                rejected_sha256=None
                if cached_sha256 is not None and cached_sha256 != r['sha256']:
                    rejected_sha256=cached_sha256
                    source.unlink()  # Refetch only the corrupt jar, including partial retry downloads.
                number=len(attempts)+1
                log=RESULTS/f'runner-dependency-{key}-{number}.txt'
                while log.exists():
                    number+=1
                    log=RESULTS/f'runner-dependency-{key}-{number}.txt'
                result=execute(['mvn','-q','-U','org.apache.maven.plugins:maven-dependency-plugin:3.6.1:get','-Dartifact='+coordinate,'-Dtransitive=false'],REPO,log,TIMEOUTS['build'])
                result.update(coordinate=coordinate, expected_sha256=r['sha256'], rejected_sha256=rejected_sha256,
                              downloaded_sha256=hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else None)
                attempts.append(result)
                write_json(evidence,dict(coordinate=coordinate,attempts=attempts))
                if not result['exit_code']:
                    break
                diagnostic=log.read_text(errors='replace')
                if retry == RUNNER_JAR_RETRIES or not environment_matches(diagnostic, RUNNER_JAR_TRANSIENT_PATTERNS):
                    raise RuntimeError('Cannot resolve runner jar '+r['filename']+'; see '+str(log.relative_to(EXP)))
                wait(RUNNER_JAR_BACKOFF_SECONDS)
        if not source.exists() or hashlib.sha256(source.read_bytes()).hexdigest()!=r['sha256']:
            raise RuntimeError('Runner jar checksum mismatch '+r['filename'])
        shutil.copy2(source,dest)
    digest=hashlib.sha256((REPO/'pom.xml').read_bytes()).hexdigest()
    maven_dependencies.resolve(REPO,EXP/'work/classpaths'/f'{digest}.txt',RESULTS/'builds/logs/runner-setup',
                               TIMEOUTS['build'],execute,prefetch=True,wait=wait)
    runtime=EXP/'work/runtime';runtime.mkdir(parents=True,exist_ok=True)
    for name in ['resources','suite']:
        source=EXP/'dev/src-test-t'/name
        if source.exists():
            shutil.copytree(source,runtime/'src/test'/name,dirs_exist_ok=True)
    write_json(RESULTS/'runner-setup.json',{'status':'completed','jars':records,'project_dependencies':digest})


def run_group(job):
    start=time.monotonic()
    index,group,phase=job['index'],job['group'],job['phase']
    rows=job['methods']
    work=EXP/job['work']
    if work.exists():shutil.rmtree(work)
    work.mkdir(parents=True)
    execfile=work/'jacoco.exec'
    coverage=phase=='B'
    by_file=collections.defaultdict(list)
    for row in rows:by_file[row['file']].append(row)
    files=[];suite_compile=None;precompiled=None
    if DEVELOPER:
        folder,cp=classpath(index)
        suite_classes=work/'suite-classes';suite_classes.mkdir()
        sources=sorted((EXP/'dev/src-test-t/java').rglob('*.java'))
        suite_compile=execute(['javac','-J-Xmx'+JVM['javac'],'-cp',cp,'-sourcepath',str(EXP/'dev/src-test-t/java'),'-d',str(suite_classes),*[str(p) for p in sources]],folder,work/'suite-javac.txt',TIMEOUTS['compile'])
        suite_compile['source_count']=len(sources)
        suite_compile['diagnostics']=(work/'suite-javac.txt').read_text(errors='replace')
        if suite_compile['exit_code']==0:precompiled=(suite_classes,suite_compile)
    for number,(filename,members) in enumerate(sorted(by_file.items())):
        files.append(run_file(index,group,filename,members,work/str(number),execfile if coverage else None,
                              repetitions=job.get('repeats',1),retry=job.get('retry',phase in ['A','B']),precompiled=precompiled,
                              environment_retry=job.get('environment_retry')))
    result=dict(index=index,sha=job['sha'],group=group,phase=phase,status='evaluated',input_sha256=job['input_sha256'],
                runner_sha256=job['runner_sha256'],files=files,methods=[m for f in files for m in f['methods']],requested_ids=[r['id'] for r in rows])
    if suite_compile is not None:result['whole_frozen_suite_compile']=suite_compile
    if coverage:
        folder,_=classpath(index);xml=EXP/job.get('coverage_xml',f'results/coverage/{index}-{group}.xml');xml.parent.mkdir(parents=True,exist_ok=True)
        result['coverage_report']=execute(['java','-Xmx'+JVM['jacoco'],'-jar',str(EXP/f'work/libs/org.jacoco.cli-{jacoco_version(folder/"target/classes")}-nodeps.jar'),'report',*([str(execfile)] if execfile.exists() else []),'--classfiles',str(folder/'target/classes'),'--sourcefiles',str(folder/'src/main/java'),'--xml',str(xml)],folder,work/'coverage-report.txt',TIMEOUTS['compile'])
        if result['coverage_report']['exit_code']==0:
            with xml.with_name(xml.stem+'-lines.csv').open('w',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=['file','line','covered'],lineterminator='\n');writer.writeheader()
                for row in line_rows(xml):
                    writer.writerow({'file':row['file'],'line':row['line'],'covered':'yes' if row['covered'] else 'no'})
        else:result['status']='coverage-fail'
    result['seconds']=round(time.monotonic()-start,3)
    write_json(EXP/job['output'],result)


def screen_file(job):
    work=EXP/job['work'];work.mkdir(parents=True,exist_ok=True)
    source=EXP/job['file']
    parse=execute(['java','-Xmx'+JVM['javac'],str(HELPERS/'SourceInventory.java'),str(source),TEST_FRAMEWORK],REPO,work/'inventory.tsv',TIMEOUTS['compile'])
    members=[]
    if parse['exit_code']==0:
        for line in (work/'inventory.tsv').read_text().splitlines():
            parts=line.split('\t')
            if parts[0]=='M' and parts[3]:
                _,cls,method,kind,eligible,signature,display,lineno,body_hash=parts
                members.append(dict(file=job['file'],**{'class':cls},method=method,kind=kind,id='|'.join([job['file'],cls,method]),body_hash=body_hash,round=job['round'],technique=job['technique']))
        result=run_file(0,job['group'],job['file'],members,work/'test',repetitions=job['repeats'],retry=False)
    else:
        result=dict(file=job['file'],methods=[],compile=None,runs=[])
    result['parse']=parse
    write_json(EXP/job['output'],result)


def repair(job):
    import zipfile
    types=job['types'];candidates=collections.defaultdict(set)
    for fqn in types:candidates[fqn.rsplit('.',1)[-1]].add(fqn)
    folder,cp=classpath(0)
    allowed={'org.junit','org.junit.jupiter.api','org.mockito','org.mockito.stubbing','org.mockito.invocation','org.hamcrest'}
    for item in cp.split(':'):
        if not item.endswith('.jar'):continue
        with zipfile.ZipFile(item) as jar:
            for path in jar.namelist():
                if not path.endswith('.class') or '/' not in path:continue
                package,name=path[:-6].rsplit('/',1)
                if package.replace('/','.') not in allowed or re.search(r'\$\d',name):continue
                fqn=(package+'.'+name).replace('/','.').replace('$','.')
                candidates[name.rsplit('$',1)[-1]].add(fqn)
    base=EXP/job['directory'];output=EXP/job['output']
    previous=json.loads(output.read_text()) if output.exists() else {'files':[]}
    records=previous['files'];done={r['file'] for r in records}
    for number,source in enumerate(sorted((base/'sources').rglob('*.java'))):
        filename=str(source.relative_to(EXP))
        if filename in done:continue
        before=source.with_suffix('.java.pre-repair')
        if before.exists():source.write_bytes(before.read_bytes())
        else:before.write_bytes(source.read_bytes())
        record=dict(file=filename,pre_repair=str(before.relative_to(EXP)),added_imports=[],passes=[])
        classes=EXP/job['work']/str(number)/'classes';classes.mkdir(parents=True,exist_ok=True)
        for attempt in range(job['passes']+1):
            log=base/f'import-repair/{number}/compile-{attempt}.txt'
            result=execute(['javac','-J-Xmx'+JVM['javac'],'-Xmaxerrs','1000','-cp',cp,'-sourcepath',str(source.parent),'-d',str(classes),str(source)],folder,log,TIMEOUTS['compile'])
            diagnostic=log.read_text(errors='replace')
            symbols=sorted(set(re.findall(r'cannot find symbol\n(?:[^\n]*\n)*?\s*symbol:\s+class\s+(\w+)',diagnostic)))
            fixes=[];ambiguous={}
            if result['exit_code'] and attempt<job['passes']:
                for symbol in symbols:
                    choices=sorted(candidates.get(symbol,[]))
                    if len(choices)==1 and choices[0] not in record['added_imports']:fixes.append(choices[0])
                    elif len(choices)>1:ambiguous[symbol]=choices
            record['passes'].append(dict(compile=result,missing_classes=symbols,added_imports=fixes,ambiguous=ambiguous))
            if not fixes:break
            text=source.read_text();imports=''.join('import '+fqn+';\n' for fqn in fixes)
            package=re.search(r'^\s*package\s+[^;]+;',text,re.M);position=package.end() if package else 0
            source.write_text(text[:position]+'\n'+imports+text[position:]);record['added_imports'].extend(fixes)
        stripped=source.read_text()
        for fqn in record['added_imports']:stripped=stripped.replace('import '+fqn+';\n','',1)
        if ''.join(stripped.split())!=''.join(before.read_text().split()):raise RuntimeError('Import repair changed non-import source')
        record['compiled_after_repair']=record['passes'][-1]['compile']['exit_code']==0
        records.append(record);write_json(output,dict(files=records,max_repair_passes=job['passes']))
    write_json(output,dict(files=records,max_repair_passes=job['passes']))


def main():
    global EXP,REPO,RESULTS,SHARED,HELPERS,JVM,TIMEOUTS,MODULE,FLAKY_RERUN,TEST_FRAMEWORK,DEVELOPER,ENVIRONMENT_ERRORS
    job=json.loads(Path(sys.argv[1]).read_text())
    EXP=Path(job['experiment']);RESULTS=EXP/'results';SHARED=EXP
    HELPERS=Path(__file__).parent/'java'
    MODULE=job['module'];REPO=EXP/'work/checkouts/0'/MODULE
    JVM=job['jvm'];TIMEOUTS=job['timeouts'];FLAKY_RERUN=job.get('flaky_rerun',1)
    TEST_FRAMEWORK=job.get('test_framework','junit5');DEVELOPER=job.get('developer',job.get('group')=='dev')
    ENVIRONMENT_ERRORS=job.get('environment_errors',{'patterns':[],'max_reruns':3,'backoff_seconds':5})
    if job['action']=='classpath':
        attempts=maven_dependencies.resolve(Path(job['folder']),EXP/job['cache'],RESULTS/'builds/logs'/f'classpath-{job["index"]}',
                                            TIMEOUTS['build'],execute,force=job['force'])
        write_json(EXP/job['output'],{'attempts':attempts})
    elif job['action']=='setup':setup()
    elif job['action']=='screen':screen_file(job)
    elif job['action']=='repair':repair(job)
    else:run_group(job)


if __name__=='__main__':main()
