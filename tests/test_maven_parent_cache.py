"""Real offline Maven models through both cache entry points; never experiments.

The tiny test plugin replaces dependency goal packaging, not model resolution:
Maven constructs the parent hierarchy, activates profiles, interpolates versions,
and injects its effective MavenProject. The fixture goals report that model and
return preseeded JAR paths, allowing a truly isolated, network-disabled repository.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import canonical_hash, validate
from test_survival.io import read_json, write_json
from test_survival.runner.cache import ClasspathCache
from test_survival.runner.docker import DockerRunner
from test_survival.selection import Selector

FIXTURE = Path(__file__).parent/'fixtures/maven-parent'
PLUGIN = '<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-dependency-plugin</artifactId><version>0-fixture</version></plugin></plugins></build>'
DEPENDENCY = '<dependencies><dependency><groupId>fixture</groupId><artifactId>library</artifactId><version>${revision}</version><scope>test</scope></dependency></dependencies>'
PROFILE = '<profiles><profile><id>marker</id><activation><file><exists>${basedir}/activate.marker</exists></file></activation><properties><revision>2</revision></properties></profile></profiles>'


def pom(name, body=''):
    return f'<project><modelVersion>4.0.0</modelVersion><groupId>fixture</groupId><artifactId>{name}</artifactId><version>1</version><packaging>pom</packaging>{body}</project>'


def parent(name, relative):
    return f'<parent><groupId>fixture</groupId><artifactId>{name}</artifactId><version>1</version><relativePath>{relative}</relativePath></parent>'


@pytest.mark.java_integration
def test_maven_parent_context_cache_acceptance(tmp_path, monkeypatch):
    image=os.environ.get('SURVIVAL_JAVA_TEST_IMAGE')
    if not image:
        if os.environ.get('SURVIVAL_REQUIRE_JAVA_TESTS')=='1':pytest.fail('Set SURVIVAL_JAVA_TEST_IMAGE')
        pytest.skip('Set SURVIVAL_JAVA_TEST_IMAGE for the offline Maven fixture')
    (tmp_path/'home').mkdir()
    compose=tmp_path/'compose.yaml'
    # Host and worker both publish fixture evidence. Use one UID on Linux too;
    # Docker Desktop otherwise hides the root-owned bind-mount boundary.
    compose.write_text(yaml.safe_dump(dict(services=dict(probe=dict(image=image,network_mode='none',
        user=f'{os.getuid()}:{os.getgid()}',environment=dict(HOME='/fixture/home',MAVEN_OPTS='-Duser.home=/fixture/home'))))))
    raw=dict(name='parent-fixture',anchor='zero',project=dict(repo=str(tmp_path),module='child',test_framework='junit5'),
             survival=dict(selection='affected',execution='batch'),
             runner=dict(docker=dict(compose_files=[str(compose)],service='probe',mounts={str(tmp_path):'/fixture',
                 str(FIXTURE.resolve()):'/fixture-sources'})))
    config=validate(raw)
    master=DockerRunner(tmp_path,config)
    master.read_only.add(str(FIXTURE.resolve()))
    evidence=[]
    evidence_path=Path(os.environ.get('SURVIVAL_PARENT_CACHE_EVIDENCE',tmp_path/'acceptance.json'))
    environment={'CACHE_PARENT_VERSION':'1'}

    class FixtureRunner(DockerRunner):
        def __init__(self, exp, settings):
            super().__init__(exp,settings)
            self.container_id=master.container_id
            self.jobs=[]
        def execute(self, command, cwd, log, timeout=600):
            if len(command)>1 and str(command[1]).endswith('/java_worker.py'):
                self.jobs.append(read_json(command[-1])['action'])
            return super().execute(['env',*[f'{k}={v}' for k,v in environment.items()],*command],cwd,log,timeout)

    with master.session('offline-maven-parent-fixture'):
        assert subprocess.check_output(['docker','inspect','--format','{{.HostConfig.NetworkMode}}',master.container_id],text=True).strip()=='none'
        result=master.execute(['python3','/fixture-sources/seed.py','/fixture-sources','/fixture/repository','/fixture/plugin-classes'],tmp_path,tmp_path/'seed.log',120)
        assert result['exit_code']==0,(tmp_path/'seed.log').read_text()
        repository_parent=tmp_path/'repository/fixture/parent/1/parent-1.pom'
        repository_parent.parent.mkdir(parents=True,exist_ok=True)
        repository_parent.write_text(pom('parent','<properties><revision>${env.CACHE_PARENT_VERSION}</revision></properties>'+DEPENDENCY+PROFILE))

        def setup(case, module='child'):
            exp=tmp_path/case
            settings=validate({**raw,'project':{**raw['project'],'module':module}})
            adapter=JavaMavenAdapter(exp,settings)
            adapter.runner=FixtureRunner(exp,settings)
            adapter.cache=ClasspathCache(exp,settings,adapter.runner)
            # Materialized fixture workspaces replace Git/build orchestration;
            # ClasspathCache.resolve, Selector.context, and both workers are real.
            monkeypatch.setattr(adapter,'checkout',lambda i,sha:exp/f'work/checkouts/{i}')
            monkeypatch.setattr(adapter,'ensure_anchor',lambda:None)
            monkeypatch.setattr(adapter,'build',lambda i,sha:read_json(exp/f'results/builds/{i}.json'))
            root=exp/'work/checkouts/0';(root/'.mvn').mkdir(parents=True)
            (root/'.mvn/maven.config').write_text('-Dmaven.repo.local=/fixture/repository\n')
            if case.startswith('repository-'):
                relative={'empty':'','missing':'../missing.xml','mismatch':'../decoy.xml'}[case.removeprefix('repository-')]
                (root/'decoy.xml').write_text(pom('wrong-parent'))
                body=parent('parent',relative)+PLUGIN
            else:
                (root/'parent').mkdir()
                (root/'parent/pom.xml').write_text(pom('parent','<properties><revision>1</revision></properties>'+DEPENDENCY+PROFILE))
                body=parent('parent','../parent/pom.xml')+PLUGIN
                if case.startswith('transitive'):
                    (root/'middle').mkdir()
                    (root/'middle/pom.xml').write_text(pom('middle',parent('parent','../parent/pom.xml')))
                    body=parent('middle','../middle/pom.xml')+PLUGIN
            modules=('a','b') if case.startswith('sibling') else ('child',)
            for name in modules:
                (root/name).mkdir()
                (root/name/'pom.xml').write_text(pom(name,body))
                (root/name/'target/classes').mkdir(parents=True)
            environment['CACHE_PARENT_VERSION']='1'
            return adapter,Selector(adapter),root

        def sample(case, stage, adapter, selector, index, expected, reuse):
            folder=adapter.folder(index);exp=adapter.exp
            output=exp/'oracle.txt';output.unlink(missing_ok=True)
            result=adapter.runner.execute(['mvn','-q','-o','dependency:observe','-Dfixture.output='+adapter.runner.path(output)],
                                           folder,exp/f'oracle-{len(evidence)}.log',60)
            assert result['exit_code']==0,Path(exp/result['log']).read_text()
            actual=output.read_text().strip();assert actual==expected
            jobs=adapter.runner.jobs
            before=jobs.count('classpath')
            _,cp,_=adapter.cache.resolve(folder,index)
            classpath_reused=jobs.count('classpath')==before
            cp_version=Path(cp.read_text().strip()).parent.name
            write_json(exp/f'results/builds/{index}.json',dict(build='ok',sha=str(index),classpath=str(cp.relative_to(exp))))
            before=jobs.count('selection-context')
            context=selector.context(index,str(index))
            selector_reused=jobs.count('selection-context')==before
            versions=[entry[2] for entry in context['entries'] if entry[:2]==['fixture','library']]
            row=dict(case=case,stage=stage,index=index,maven_version=actual,classpath_version=cp_version,
                     context_versions=versions,classpath_reused=classpath_reused,selector_reused=selector_reused,
                     input_reasons=read_json(exp/f'work/context-inputs/{index}/inputs.json')['activation']['unbounded'])
            evidence.append(row);write_json(evidence_path,evidence)
            assert cp_version==actual and versions==[actual] and context['known'],row
            assert (classpath_reused,selector_reused)==reuse,row
            return cp,context

        for case in ('local','local-remove','transitive','transitive-remove','sibling-a','sibling-b','repository-empty','repository-missing','repository-mismatch','positive','legacy-resume'):
            module=case[-1] if case.startswith('sibling') else 'child'
            adapter,selector,root=setup(case,module)
            removing=case in ('local-remove','transitive-remove')
            if removing:(root/'child/activate.marker').touch()
            cp,context=sample(case,'initial',adapter,selector,0,'2' if removing else '1',(False,False))
            if case=='legacy-resume':
                inputs=read_json(adapter.exp/'work/context-inputs/0/inputs.json')
                manifest=adapter.helpers/'classpath-artifacts.json'
                image_id=subprocess.check_output(['docker','inspect','--format','{{.Image}}',master.container_id],text=True).strip()
                legacy=canonical_hash(dict(version=5,inputs=inputs,image=image_id,module=module,
                    runner_libraries=hashlib.sha256(manifest.read_bytes()).hexdigest()))
                legacy_cp=cp.with_name(legacy+'.txt');cp.rename(legacy_cp)
                context.update(input_sha256=legacy,classpath=str(legacy_cp.relative_to(adapter.exp)))
                write_json(adapter.exp/'results/selection-context/0.json',context)
                sample(case,'v5-same-index-resume',adapter,Selector(adapter),0,'1',(False,False))
                (root/'child/activate.marker').touch()
                sample(case,'changed-same-index-resume',adapter,Selector(adapter),0,'2',(False,False))
                continue
            next_root=adapter.exp/'work/checkouts/1';shutil.copytree(root,next_root)
            marker=next_root/('a' if case.startswith('sibling') else 'child')/'activate.marker'
            if case=='positive':
                sample(case,'unchanged-next-index',adapter,selector,1,'1',(True,True))
                sample(case,'unchanged-same-index-resume',adapter,Selector(adapter),1,'1',(True,True))
                continue
            if removing:marker.unlink()
            else:marker.touch()
            sample(case,'child-marker-removed' if removing else 'child-marker-created',adapter,selector,1,
                   '1' if removing or case=='sibling-b' else '2',(False,False))
            if case.startswith('sibling'):continue
            if case.startswith('repository'):
                marker.unlink()
                sample(case,'child-marker-removed-same-object',adapter,selector,1,'1',(False,False))
                environment['CACHE_PARENT_VERSION']='3'
                sample(case,'parent-only-env-changed',adapter,Selector(adapter),1,'3',(False,False))
                sample(case,'unchanged-repeat-same-object',adapter,selector,1,'3',(False,False))
            else:
                sample(case,'unchanged-repeat-same-object',adapter,selector,1,'1' if removing else '2',(True,True))
