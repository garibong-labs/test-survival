#!/usr/bin/env python3
"""Scratch-only Maven references and serial per-index timing reconstruction."""
import argparse
from collections import defaultdict
from datetime import datetime
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from statistics import median
from xml.sax.saxutils import escape


def worker_prepare(exp):
    sys.path.insert(0,'/survival-package')
    from adapters import java_worker as w
    from adapters.snapshot_compile import filtered_roots
    from adapters.dependencies import class_inventory
    from adapters.policy_coverage import includes_pattern
    w.EXP=w.SHARED=exp;w.RESULTS=exp/'results';w.HELPERS=Path('/survival-package/adapters/java')
    job=json.loads((exp/'reference-input.json').read_text())
    w.MODULE=job['module'];w.JVM=job['jvm'];w.TIMEOUTS=job['timeouts']
    root=exp/'reference';root.mkdir(exist_ok=True)
    methods=filtered_roots(exp,job['methods'],root/'sources',job['excluded'])
    folder,cp=w.classpath(0);version=w.jacoco_version(folder/'target/classes')
    agent=exp/f'work/libs/org.jacoco.agent-{version}-runtime.jar'
    cp+=':'+str(exp/f'work/libs/org.jacoco.cli-{version}-nodeps.jar')+':'+str(agent)
    helpers=root/'helpers';helper_classes=root/'helper-classes';helper_classes.mkdir()
    subprocess.run(['javac','-cp',cp,'-d',str(helper_classes),str(helpers/'FrozenRunner.java')],check=True)
    test_classes=root/'target/test-classes';test_classes.mkdir(parents=True)
    subprocess.run(['javac','-cp',cp,'-d',str(test_classes),str(helpers/'FrozenSnapshotTest.java')],check=True)
    plugin_classes=root/'plugin-classes';plugin_classes.mkdir()
    maven_home=Path(shutil.which('mvn')).resolve().parents[1]
    subprocess.run(['javac','-cp',str(maven_home/'lib/*'),'-d',str(plugin_classes),str(helpers/'SnapshotCompileMojo.java')],check=True)
    descriptor=plugin_classes/'META-INF/maven/plugin.xml';descriptor.parent.mkdir(parents=True)
    descriptor.write_text('''<plugin><name>Frozen snapshot reference compiler</name><groupId>reference</groupId><artifactId>snapshot-compiler</artifactId><version>1</version><goalPrefix>frozen</goalPrefix><isolatedRealm>false</isolatedRealm><inheritedByDefault>false</inheritedByDefault><mojos><mojo><goal>compile</goal><implementation>reference.SnapshotCompileMojo</implementation><language>java</language><phase>test-compile</phase><instantiationStrategy>per-lookup</instantiationStrategy><executionStrategy>once-per-session</executionStrategy><requiresProject>true</requiresProject><threadSafe>false</threadSafe><parameters/></mojo></mojos><dependencies/></plugin>''')
    repository=Path('/root/.m2/repository/reference/snapshot-compiler/1');repository.mkdir(parents=True,exist_ok=True)
    subprocess.run(['jar','--create','--file',str(repository/'snapshot-compiler-1.jar'),'-C',str(plugin_classes),'.'],check=True)
    (repository/'snapshot-compiler-1.pom').write_text('<project><modelVersion>4.0.0</modelVersion><groupId>reference</groupId><artifactId>snapshot-compiler</artifactId><version>1</version><packaging>maven-plugin</packaging></project>')
    compiled={};groups=defaultdict(list)
    for t in methods:
        source=exp/t['source_root']
        if source not in compiled:compiled[source]=root/f'snapshot-classes/{len(compiled)}'
        groups[source,t['execution_hash']].append(t)
    (root/'compile.tsv').write_text(''.join(f'{source}\t{classes}\t{cp}\n' for source,classes in compiled.items()))
    manifest=[]
    for number,((source,digest),ts) in enumerate(groups.items()):
        selectors=root/f'selectors-{number}.tsv'
        selectors.write_text(''.join(c+'\t'+m+'\n' for c,m in sorted({(t['class'],t['method']) for t in ts})))
        resources=exp/ts[0]['resource_root']
        runtime=':'.join(map(str,[compiled[source],helper_classes,cp,resources/'resources',resources/'suite']))
        manifest.append(f'{selectors}\t{runtime}\t{resources}\n')
    (root/'run.tsv').write_text(''.join(manifest))
    included=(':'.join(n.replace('/','.') for n in sorted(class_inventory(folder/'target/classes')))
              if job['mode']=='affected' else includes_pattern(job['scope_classes']))
    argument=f'-Xmx{job["jvm"]["jacoco"]} -javaagent:{agent}=output=none,dumponexit=false,includes={included}'
    elements=''.join('<additionalClasspathElement>'+escape(p)+'</additionalClasspathElement>' for p in cp.split(':'))
    (root/'pom.xml').write_text(f'''<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>reference</groupId><artifactId>frozen-tests</artifactId><version>1</version>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>system</scope><systemPath>{exp}/work/libs/junit-4.13.2.jar</systemPath></dependency></dependencies>
<build><plugins>
<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-resources-plugin</artifactId><version>3.3.1</version></plugin>
<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version></plugin>
<plugin><groupId>reference</groupId><artifactId>snapshot-compiler</artifactId><version>1</version><executions><execution><phase>test-compile</phase><goals><goal>compile</goal></goals></execution></executions></plugin>
<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId><version>3.5.4</version><configuration><forkCount>1</forkCount><reuseForks>true</reuseForks><includes><include>FrozenSnapshotTest</include></includes><argLine>{escape(argument)}</argLine><additionalClasspathElements>{elements}</additionalClasspathElements></configuration></plugin>
</plugins></build></project>''')
    # Resolve provider/plugin dependencies before the timed offline command.
    subprocess.run(['mvn','-q','-DskipTests','-Dref.skip=true','test'],cwd=root,check=True)
    subprocess.run(['mvn','-q','dependency:get','-Dartifact=org.apache.maven.surefire:surefire-junit4:3.5.4'],cwd=root,check=True)
    (root/'agent-settings.txt').write_text(argument+'\n')


def reference(root,mode):
    from test_survival.config import load,as_written
    from test_survival.io import read_json,write_json
    from test_survival.adapters.java_maven import JavaMavenAdapter
    from test_survival.stages.batch import Measurements
    import yaml
    source=root/'repo/experiments'/mode;exp=root/f'reference-{mode}'
    exp.mkdir()  # Never overwrite a measured reference.
    config=load(source);config['name']='reference-'+config['name']
    volume='affected-reference-'+read_json(root/'setup.json')['project']+'-'+mode+'-m2'
    subprocess.run(['docker','run','--rm','-v',config['runner']['docker']['m2_volume']+':/source:ro','-v',volume+':/target','alpine','sh','-c','cp -a /source/. /target/'],check=True)
    config['runner']['docker']['m2_volume']=volume
    saved=as_written(config,include_defaults=True);saved['dev'].pop('denominator',None)
    (exp/'experiment.yaml').write_text(yaml.safe_dump(saved,sort_keys=False))
    for name in ('dev','generated','population'):shutil.copytree(source/name,exp/name)
    for name in ('anchor.json','frozen-denominators.json'):write_json(exp/'results'/name,read_json(source/'results'/name))
    measurement=Measurements(exp,config,'control')
    write_json(exp/'reference-input.json',dict(module=config['project']['module'],mode=mode,jvm=config['runner']['jvm'],timeouts=config['runner']['timeouts'],
                methods=list(measurement.unique.values()),excluded=sorted(measurement.excluded),scope_classes=[c['class'] for c in measurement.scope]))
    shutil.copyfile(__file__,exp/'reference-worker.py')
    shutil.copytree(Path(__file__).with_name('affected_reference'),exp/'reference/helpers')
    adapter=measurement.adapter
    with adapter.runner.session('reference'):
        if adapter.build(0,read_json(exp/'results/anchor.json')['sha'])['build']!='ok':raise RuntimeError('Reference production build failed')
        adapter.setup_runner()
        prepared=adapter.runner.execute(['python3',str(exp/'reference-worker.py'),'worker-prepare','--root',str(exp)],adapter.folder(0),exp/'reference-prepare.log',900)
        if prepared['exit_code']:raise RuntimeError('Reference setup failed; see '+str(exp/'reference-prepare.log'))
        measure_reference(root,mode,adapter,len(measurement.unique))


def measure_reference(root,mode,adapter,expected):
    from test_survival.io import write_json
    exp=adapter.exp
    started=time.monotonic()
    result=adapter.runner.execute(['mvn','-o','-q','test'],exp/'reference',exp/'reference-test.log',900)
    wall=time.monotonic()-started
    if result['exit_code']:raise RuntimeError('Maven reference failed; see '+str(exp/'reference-test.log'))
    rows=[line.split('\t') for line in (exp/'reference/observations.tsv').read_text().splitlines()]
    count=sum(int(r[1]) for r in rows)
    assert count==expected,(count,expected)
    summary=dict(mode=mode,command=['mvn','-o','-q','test'],seconds=wall,
                 compile_seconds=float((exp/'reference/compile-seconds.txt').read_text()),
                 test_seconds=float((exp/'reference/test-seconds.txt').read_text()),methods=count,
                 surefire_pid=(exp/'reference/surefire-pid.txt').read_text(),
                 agent=(exp/'reference/agent-settings.txt').read_text().strip(),
                 sources_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (exp/'reference/helpers').glob('*.java')},
                 tool_head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip())
    write_json(root/f'reference-{mode}.json',summary);print(json.dumps(summary,indent=2))


def timing(root):
    from test_survival.io import read_json,read_csv,write_csv,write_json
    def stamp(line):return datetime.fromisoformat(line.split()[0])
    result={}
    for mode in ('all','affected'):
        exp=root/'repo/experiments'/mode
        # Both comparison modes have one worker: event spans are non-overlapping.
        build={};previous=None
        for line in (root/f'{mode}-prepare-scope.log').read_text().splitlines():
            if 'prepare:' not in line:continue
            current=stamp(line);match=re.search(r'prepare: (?:build (\d+)/\d+|anchor build)',line)
            if match:build[int(match[1] or 0)]=(current-previous).total_seconds()
            previous=current
        spans={};previous=None
        for line in (root/f'{mode}-survive.log').read_text().splitlines():
            if 'survive: started' in line:previous=stamp(line)
            match=re.search(r'survive: (\d+) batch recorded',line)
            if match:
                current=stamp(line);spans[int(match[1])]=(current-previous).total_seconds();previous=current
        stages=read_json(root/f'{mode}-timing.json')['steps']
        spans[0]=next(s['seconds'] for s in stages if s['stage']=='control')
        reference=read_json(root/f'reference-{mode}.json')['seconds'];rows=[]
        for index,wall in sorted(spans.items()):
            raw=read_json(exp/f'results/baseline-batches/{index}.json')
            compilations={a['log']:a['seconds'] for c in raw['compile'] for a in c.get('attempts',[c])}
            compile_seconds=sum(compilations.values());jvm=sum(r['seconds'] for r in raw['runs'])
            counts=raw['selection_counts'];full=counts['executed']>0 and not counts['carried']
            rows.append(dict(index=index,sha=raw['sha'],production_build_seconds=build.get(index,0),
                test_compile_seconds=compile_seconds,test_jvm_seconds=jvm,other_seconds=wall-compile_seconds-jvm,
                excluding_build_seconds=wall,total_seconds=wall+build.get(index,0),
                executed=counts['executed'],carried=counts['carried'],full_execution=full,
                reference_seconds=reference,ratio=wall/reference if full else ''))
        write_csv(root/f'{mode}-per-index-performance.csv',rows,list(rows[0]))
        full=[r for r in rows if r['full_execution']]
        result[mode]=dict(reference_seconds=reference,full_execution_indices=[r['index'] for r in full],
                          max_ratio=max(r['ratio'] for r in full),median_ratio=median(r['ratio'] for r in full),
                          over_target=[r['index'] for r in full if r['ratio']>1.5])
    write_json(root/'performance-summary.json',result);print(json.dumps(result,indent=2))


def policy_timing(root):
    """Serial shared physical work; no Maven target for regenerated segments."""
    from test_survival.io import read_json,read_csv,write_csv,resolve_path,logical_glob
    for mode in ('all','affected'):
        exp=root/f'repo/experiments/{mode}-policies';batches=defaultdict(list)
        for path in logical_glob(exp/'results/policy/batches','*.json'):
            batch=read_json(path);batches[int(batch['index'])].append(batch)
        spans=defaultdict(float)
        if mode=='all':
            # Regeneration can rewrite a child's state at the same index.
            # Keep the complete chronological interval from the append-only
            # events, including both its evaluation and boundary measurements.
            ended={}
            for line in (root/f'{mode}-policies-run.log').read_text().splitlines():
                match=re.search(r'policy-run-[^:]+-(\d+): (?:completed|already completed; skipped)',line)
                if match:ended[int(match[1])]=datetime.fromisoformat(line.split()[0]).timestamp()
            previous=datetime.fromisoformat(read_json(exp/'state/policy-survive.json')['started']).timestamp()
            for index,end in sorted(ended.items()):spans[index]=end-previous;previous=end
        else:
            ended={}
            for folder in (exp/'results/policy').glob('*/decisions/*/*'):
                for path in logical_glob(folder,'*.json'):
                    index=int(read_json(path)['index']);ended[index]=max(ended.get(index,0),resolve_path(path).stat().st_mtime)
            previous=datetime.fromisoformat(read_json(exp/'state/policy-survive.json')['started']).timestamp()
            for index,end in sorted(ended.items()):spans[index]=end-previous;previous=end
        rows=[];first=min(batches,default=None)
        for commit in read_csv(root/f'repo/experiments/{mode}/results/survival.csv'):
            index=int(commit['index'])
            if not index or commit['evaluated']!='yes':continue
            attempts={a['log']:a['seconds'] for b in batches[index] for c in b.get('compile',[]) for a in c.get('attempts',[c])}
            compilation=sum(attempts.values());jvm=sum(r['seconds'] for b in batches[index] for r in b.get('runs',[]))
            production=sum(a['seconds'] for a in read_json(exp/f'results/builds/{index}.json',{}).get('attempts',[]))
            if index==first:production+=sum(a['seconds'] for a in read_json(exp/'results/builds/0.json',{}).get('attempts',[]))
            wall=spans[index]
            rows.append(dict(index=index,sha=commit['sha'],production_build_seconds=production,
                test_compile_seconds=compilation,test_jvm_seconds=jvm,other_seconds=wall-production-compilation-jvm,
                total_seconds=wall,physical_methods=sum(len(b.get('methods',[])) for b in batches[index])))
        write_csv(root/f'{mode}-policies-per-index-performance.csv',rows,list(rows[0]))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['reference','worker-prepare','timing','policy-timing']);parser.add_argument('--root',type=Path,required=True);parser.add_argument('--mode',choices=['all','affected'])
    args=parser.parse_args()
    if args.action=='worker-prepare':worker_prepare(args.root)
    elif args.action=='reference':reference(args.root.resolve(),args.mode)
    elif args.action=='policy-timing':policy_timing(args.root)
    else:timing(args.root)
