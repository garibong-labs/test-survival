from types import SimpleNamespace

from test_survival.adapters.context_inputs import collect
from test_survival.io import read_json, write_json
from test_survival.selection import Selector


def test_previous_evaluated_context_reused_until_inputs_change(tmp_path):
    calls=[]
    cp=tmp_path/'classpath.txt';cp.write_text('')
    inputs=['first']
    def worker(job):
        calls.append(job['index'])
        return dict(version=1,sha=job['sha'],known=True,entries=[['group','artifact','1','','jar']])
    adapter=SimpleNamespace(exp=tmp_path,config=dict(project=dict(repo=str(tmp_path),module='.')),
        checkout=lambda *a:None,folder=lambda _:tmp_path,worker=worker,
        cache=SimpleNamespace(context_identity=lambda _:inputs[0],resolve=lambda *a:(None,cp,[])))
    selector=Selector(adapter)
    assert selector.context(0,'t')['action']=='resolved'
    assert selector.context(2,'two')['action']=='reused'
    # A new process uses durable previous-index evidence too.
    selector=Selector(adapter)
    assert selector.context(5,'five')['reused_from_index']==2
    inputs[0]='changed-settings-image-or-pom'
    assert selector.context(7,'seven')['action']=='resolved'
    assert selector.context(9,'nine')['action']=='reused'
    assert calls==[0,7]
    assert read_json(tmp_path/'results/selection-context/9.json')['sha']=='nine'


def test_unknown_context_cannot_be_reused(tmp_path):
    calls=[]
    cp=tmp_path/'classpath.txt';cp.write_text('')
    adapter=SimpleNamespace(exp=tmp_path,config=dict(project=dict(repo=str(tmp_path),module='.')),
        checkout=lambda *a:None,folder=lambda _:tmp_path,
        cache=SimpleNamespace(context_identity=lambda _:'key',resolve=lambda *a:(None,cp,[])))
    def worker(job):
        calls.append(job['index']);return dict(version=1,known=False,sha=job['sha'],entries=[])
    adapter.worker=worker
    selector=Selector(adapter)
    selector.context(0,'t');selector.context(1,'next')
    assert calls==[0,1]


def test_all_poms_maven_settings_and_configuration_are_content_inputs(tmp_path,monkeypatch):
    home=tmp_path/'home';home.mkdir();monkeypatch.setenv('HOME',str(home))
    repo=tmp_path/'repo';repo.mkdir();(repo/'pom.xml').write_text('<project/>')
    child=repo/'nested/module';child.mkdir(parents=True);(child/'pom.xml').write_text('<project/>')
    initial=collect(repo)
    (child/'Other.java').write_text('changed source')
    assert collect(repo)==initial
    (child/'pom.xml').write_text('<project><version>2</version></project>')
    changed=collect(repo);assert changed!=initial
    (home/'.m2').mkdir();(home/'.m2/settings.xml').write_text('SECRET')
    settings=collect(repo);assert settings!=changed
    assert 'SECRET' not in str(settings)
    (repo/'.mvn').mkdir();(repo/'.mvn/maven.config').write_text('--settings other-settings.xml')
    (repo/'other-settings.xml').write_text('FIRST')
    explicit=collect(repo)
    (repo/'other-settings.xml').write_text('SECOND')
    assert collect(repo)!=explicit
    (child/'pom.xml').unlink();assert collect(repo)['poms_and_maven'].keys()!=initial['poms_and_maven'].keys()


def test_real_identity_changes_for_runner_image_and_nested_pom(tmp_path,monkeypatch):
    from test_survival.config import validate
    from test_survival.runner.cache import ClasspathCache
    import test_survival.runner.cache as cache_module
    config=validate(dict(name='x',anchor='t',project=dict(repo=str(tmp_path)),runner=dict(docker=dict(image='runner:tag'))))
    image=['sha256:first']
    monkeypatch.setattr(cache_module.subprocess,'check_output',lambda *a,**kw:image[0])
    def execute(command,cwd,log,timeout):
        write_json(command[-1],collect(command[-3],command[-2]))
        return dict(exit_code=0)
    runner=SimpleNamespace(execute=execute,container_id=None)
    cache=ClasspathCache(tmp_path,config,runner)
    for index in (0,2):
        root=tmp_path/f'work/checkouts/{index}';(root/'sibling').mkdir(parents=True)
        (root/'pom.xml').write_text('<project/>');(root/'sibling/pom.xml').write_text('<project/>')
    before=cache.context_identity(0)
    assert cache.context_identity(2)==before
    image[0]='sha256:replacement'
    assert cache.context_identity(2)!=before
    image[0]='sha256:first'
    (tmp_path/'work/checkouts/2/sibling/pom.xml').write_text('<project><version>2</version></project>')
    assert cache.context_identity(2)!=before


def test_reactor_classpath_paths_are_not_carried_into_next_checkout(tmp_path):
    first=tmp_path/'first.txt';first.write_text('/experiment/work/checkouts/0/module/target/classes')
    second=tmp_path/'second.txt';second.write_text('/experiment/work/checkouts/1/module/target/classes')
    resolutions=[]
    def resolve(folder,index):
        resolutions.append(index);return None,(first if index==0 else second),[]
    adapter=SimpleNamespace(exp=tmp_path,config=dict(project=dict(repo=str(tmp_path),module='.')),
        checkout=lambda *a:None,folder=lambda _:tmp_path,
        runner=SimpleNamespace(path=lambda p:'/experiment/work/checkouts'),
        cache=SimpleNamespace(context_identity=lambda _:'same-poms-settings-image',resolve=resolve),
        worker=lambda job:dict(version=1,known=True,sha=job['sha'],entries=[]))
    selector=Selector(adapter)
    selector.context(0,'t')
    assert selector.context(1,'next')['action']=='reused'
    assert resolutions==[0]
    path=tmp_path/read_json(tmp_path/'work/selection-classpaths/1.json')['classpath']
    assert path.read_text()==second.read_text()


def test_settings_under_overridden_java_home_are_content_inputs(tmp_path,monkeypatch):
    repo=tmp_path/'repo';repo.mkdir();(repo/'pom.xml').write_text('<project/>')
    home=tmp_path/'effective-home';(home/'.m2').mkdir(parents=True)
    settings=home/'.m2/settings.xml';settings.write_text('first')
    monkeypatch.setenv('MAVEN_OPTS','-Duser.home='+str(home))
    before=collect(repo)
    settings.write_text('second')
    assert collect(repo)!=before


def test_reactor_classpath_template_is_retained_without_maven_reresolution(tmp_path):
    from test_survival.config import validate
    from test_survival.runner.cache import ClasspathCache
    config=validate(dict(name='x',anchor='t',project=dict(repo=str(tmp_path)),survival=dict(selection='affected',execution='batch')))
    resolutions=[]; workers=[]
    def execute(command,cwd,log,timeout):
        workers.append(command)
        job=read_json(command[-1]);cache=tmp_path/job['cache']
        if not cache.exists():
            resolutions.append(job['index']);cache.write_text('/experiment/work/checkouts/0/module/target/classes:/root/.m2/artifact.jar')
        write_json(tmp_path/job['output'],dict(attempts=[]))
        return dict(exit_code=0)
    runner=SimpleNamespace(execute=execute,path=lambda p:str(p).replace(str(tmp_path),'/experiment'))
    cache=ClasspathCache(tmp_path,config,runner);cache.context_identity=lambda index:'same'
    for index in (0,2):
        folder=tmp_path/f'work/checkouts/{index}';folder.mkdir(parents=True);(folder/'pom.xml').write_text('<project/>')
        _,path,_=cache.resolve(folder,index)
        assert path.read_text()==f'/experiment/work/checkouts/{index}/module/target/classes:/root/.m2/artifact.jar'
    assert resolutions==[0]
    assert len(workers)==1
    assert (tmp_path/'work/classpaths/same.txt').exists()


def test_profile_activation_tracks_marker_existence_and_environment(tmp_path,monkeypatch):
    repo=tmp_path/'repo';repo.mkdir()
    (repo/'pom.xml').write_text('''<project><profiles><profile><activation>
      <file><exists>${basedir}/activate.marker</exists></file>
      <property><name>env.SURVIVAL_PROFILE</name><value>enabled</value></property>
    </activation></profile></profiles><properties><version>${env.SURVIVAL_VERSION}</version></properties></project>''')
    initial=collect(repo)
    (repo/'activate.marker').touch()
    active=collect(repo)
    assert active!=initial
    (repo/'activate.marker').write_text('content does not affect activation')
    assert collect(repo)==active
    monkeypatch.setenv('SURVIVAL_PROFILE','enabled')
    environment=collect(repo);assert environment!=active
    monkeypatch.setenv('SURVIVAL_VERSION','secret-version')
    changed=collect(repo);assert changed!=environment
    assert 'secret-version' not in str(changed)


def test_unknown_activation_interpolation_disables_reuse(tmp_path):
    (tmp_path/'pom.xml').write_text('<project><profiles><profile><activation><file><missing>${unknown.path}/marker</missing></file></activation></profile></profiles></project>')
    assert collect(tmp_path)!=collect(tmp_path)


def test_build_only_compose_uses_resolved_service_image(tmp_path,monkeypatch):
    from test_survival.config import validate
    from test_survival.runner.cache import ClasspathCache
    import test_survival.runner.cache as module
    compose=tmp_path/'compose.yaml';compose.write_text('services:\n  runner:\n    build: .\n')
    config=validate(dict(name='x',anchor='t',project=dict(repo=str(tmp_path)),
        runner=dict(docker=dict(compose_files=[str(compose)],service='runner'))))
    folder=tmp_path/'work/checkouts/0';folder.mkdir(parents=True);(folder/'pom.xml').write_text('<project/>')
    commands=[]
    def output(command,**kw):
        commands.append(command)
        if 'compose' in command:
            if '--images' in command:return 'fixture-runner\n'
            return '{"services":{"runner":{"build":{"context":"."}}}}'
        assert command[-1]=='fixture-runner'
        return 'sha256:actual-image\n'
    monkeypatch.setattr(module.subprocess,'check_output',output)
    def execute(command,cwd,log,timeout):
        write_json(command[-1],collect(command[-3],cwd));return dict(exit_code=0)
    cache=ClasspathCache(tmp_path,config,SimpleNamespace(container_id=None,execute=execute))
    assert cache.context_identity(0)
    assert any('--images' in c and c[-1]=='runner' for c in commands)


def test_unbounded_system_property_activation_is_not_reused(tmp_path,monkeypatch):
    (tmp_path/'pom.xml').write_text('<project><profiles><profile><activation><property><name>user.dir</name><value>/some/index</value></property></activation></profile></profiles></project>')
    assert collect(tmp_path)!=collect(tmp_path)
    monkeypatch.setenv('MAVEN_OPTS','-Duser.dir=/explicit/path')
    assert collect(tmp_path)==collect(tmp_path)


def test_module_local_maven_configuration_and_relative_settings(tmp_path):
    repo=tmp_path/'repo';module=repo/'module';(module/'.mvn').mkdir(parents=True)
    (repo/'pom.xml').write_text('<project/>');(module/'pom.xml').write_text('<project/>')
    config=module/'.mvn/maven.config';config.write_text('-Drevision=1\n--settings local-settings.xml\n')
    settings=module/'local-settings.xml';settings.write_text('<settings/>')
    first=collect(repo,module)
    config.write_text('-Drevision=2\n--settings local-settings.xml\n')
    second=collect(repo,module);assert second!=first
    settings.write_text('<settings><offline>true</offline></settings>')
    third=collect(repo,module);assert third!=second
    (module/'.mvn/jvm.config').write_text('-Duser.home=custom-home')
    home=module/'custom-home/.m2';home.mkdir(parents=True)
    (home/'settings.xml').write_text('<settings/>')
    before=collect(repo,module)
    (home/'settings.xml').write_text('<settings><offline>true</offline></settings>')
    assert collect(repo,module)!=before
    # Equivalent module inputs at another evaluated index must still reuse.
    import shutil
    next_checkout=tmp_path/'next-checkout';shutil.copytree(repo,next_checkout)
    assert collect(next_checkout,next_checkout/'module')==collect(repo,module)


def test_module_uses_nearest_maven_directory_and_invocation_relative_settings(tmp_path):
    repo=tmp_path/'repo';module=repo/'module';module.mkdir(parents=True);(repo/'.mvn').mkdir()
    (repo/'pom.xml').write_text('<project/>');(module/'pom.xml').write_text('<project/>')
    (repo/'.mvn/maven.config').write_text('--settings settings.xml\n')
    (repo/'settings.xml').write_text('<settings/>')
    (module/'settings.xml').write_text('<settings/>')
    inherited=collect(repo,module)
    (repo/'settings.xml').write_text('<settings><offline>true</offline></settings>')
    assert collect(repo,module)==inherited
    (module/'settings.xml').write_text('<settings><offline>true</offline></settings>')
    changed=collect(repo,module);assert changed!=inherited
    # An empty .mvn still shadows the ancestor configuration.
    (module/'.mvn').mkdir()
    local=collect(repo,module);assert local!=changed
    assert local['maven_base']=='module' and inherited['maven_base']=='.'


def test_explicit_maven_basedir_is_collected(tmp_path,monkeypatch):
    repo=tmp_path/'repo';repo.mkdir();(repo/'pom.xml').write_text('<project/>')
    external=tmp_path/'external';(external/'.mvn').mkdir(parents=True)
    config=external/'.mvn/maven.config';config.write_text('-Drevision=1')
    monkeypatch.setenv('MAVEN_BASEDIR',str(external))
    before=collect(repo)
    config.write_text('-Drevision=2')
    assert collect(repo)!=before
