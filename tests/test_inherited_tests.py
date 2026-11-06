from test_survival.adapters.inherited_tests import expand
from test_survival.adapters.build_discovery import report_methods
from test_survival.rules.dev_denominator import denominator, coverage_scoped, frozen_method
from test_survival.policy.artifacts import snapshot


def inventory():
    classes = {
        'p.Base': dict(kind='CLASS', package='p', modifiers='abstract', path='dev/src-test-t/java/p/Base.java'),
        'p.Middle': dict(kind='CLASS', package='p', modifiers='abstract', extends='Base<String>', path='dev/src-test-t/java/p/Middle.java'),
        'p.One': dict(kind='CLASS', package='p', extends='Middle', path='dev/src-test-t/java/p/One.java'),
        'q.Two': dict(kind='CLASS', package='q', extends='Middle', imports='p.Middle', path='dev/src-test-t/java/q/Two.java'),
    }
    methods = [dict(**{'class':'p.Base'}, method=name, kind='plain', signature=name+'()', modifiers=flags, body_hash=name)
               for name, flags in [('publicTest','public'),('protectedTest','protected'),('packageTest',''),('privateTest','private')]]
    methods.append(dict(**{'class':'p.One'}, method='publicTest', kind='', signature='publicTest()', modifiers='public'))
    return dict(classes=classes, methods=methods)


def test_concrete_identity_multilevel_imports_visibility_and_overrides():
    result = expand(inventory())
    recovered = [m for m in result['methods'] if m.get('inherited')]
    assert {(m['class'],m['method']) for m in recovered} == {
        ('p.One','protectedTest'),('p.One','packageTest'),('q.Two','publicTest'),('q.Two','protectedTest')}
    assert all(m['declaring_class']=='p.Base' and m['declaring_file'].endswith('/p/Base.java') for m in recovered)


def test_build_mapping_and_denominators_keep_inherited_identity(tmp_path):
    tests = [m for m in expand(inventory())['methods'] if m['kind']]
    (tmp_path/'TEST-q.Two.xml').write_text('<testsuite><testcase classname="q.Two" name="protectedTest()"/></testsuite>')
    observed, unmapped, counts = report_methods(tmp_path,tests)
    assert observed == {('q.Two','protectedTest'):['pass']} and not unmapped
    row = next(m for m in tests if m['class']=='q.Two' and m['method']=='protectedTest')
    selected = denominator([dict(row,stable='yes')],'project',[],set())
    covered, excluded = coverage_scoped([dict(row,stable='yes')],[dict(row,status='pass',scope_lines_covered=1)])
    assert covered == selected and not excluded
    test = frozen_method(selected[0])
    assert test['class']=='q.Two' and test['file'].endswith('/q/Two.java')
    assert test['declaring_file'].endswith('/p/Base.java')
    for name in ('p/Base','q/Two'):
        path = tmp_path/('dev/src-test-t/java/'+name+'.java'); path.parent.mkdir(parents=True,exist_ok=True);path.write_text(name)
    pool = snapshot(tmp_path,tmp_path/'policy',[test],group='dev',method='dev',scope=[],prefix='pool')
    assert pool[0]['id']=='q.Two#protectedTest'
    assert pool[0]['declaring_file']=='pool/dev/src-test-t/java/p/Base.java'


def test_nested_inheritance_and_overloads_stay_ambiguous(tmp_path):
    inv=inventory()
    inv['classes']['p.Outer$Inner']=dict(kind='CLASS',package='p',extends='Base',path='dev/src-test-t/java/p/Outer.java')
    inv['methods'].append(dict(inv['methods'][0],signature='publicTest(int)'))
    methods=[m for m in expand(inv)['methods'] if m['kind']]
    (tmp_path/'TEST-inner.xml').write_text('<testsuite><testcase classname="p.Outer$Inner" name="publicTest[1]"/></testsuite>')
    observed,unmapped,_=report_methods(tmp_path,methods)
    assert not observed and unmapped[0]['reason']=='ambiguous-source-method'


def test_regeneration_build_filter_preserves_concrete_inherited_pool_members(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from test_survival.policy import pools
    from test_survival.adapters import build_discovery
    from test_survival.config import validate
    from test_survival.io import write_json
    config=validate(dict(name='pool',anchor='t',project=dict(repo=str(tmp_path)),dev=dict(discovery='build')))
    source=tmp_path/'checkout/src/test/java/p';source.mkdir(parents=True)
    for name in ('Base','One','Two','Excluded'):(source/(name+'.java')).write_text('class '+name+' {}')
    methods=[dict(**{'class':'p.'+name},method='works',kind='plain',body_hash='same-inherited-body',
                  declaring_class='p.Base',declaring_file='/experiment/dev/src-test-t/java/p/Base.java') for name in ('One','Two')]
    methods.append(dict(**{'class':'p.Excluded'},method='tagged',kind='plain',body_hash='excluded'))
    selected=[]
    class Adapter:
        def __init__(self,exp,config): self.exp=exp; self.config=config; self.runner=SimpleNamespace(path=lambda p:'/experiment')
        def build(self,*args): return {'build':'ok'}
        def folder(self,*args): return tmp_path/'checkout'
        def setup_runner(self): pass
        def list_production_methods(self,*args): return dict(classes={'p.Foo':{}},methods=[])
        def list_test_methods(self,*args): return dict(classes={},methods=[dict(m) for m in methods])
        def provenance(self): return {}
        def worker(self,job):
            selected.extend(job['methods'])
            return dict(methods=[dict(t,state='pass') for t in job['methods']])
    monkeypatch.setattr(pools,'JavaMavenAdapter',Adapter)
    monkeypatch.setattr(pools,'pool_config',lambda b,p:config)
    def discover(adapter,candidates):
        assert len(candidates)==3
        return [m for m in candidates if m['method']=='works']
    monkeypatch.setattr(build_discovery,'discover',discover)
    def coverage(adapter,index,members,*a,**kw):
        assert kw==dict(full_suite=False,candidate_suite=True)
        return dict(methods=[dict(t,lines=[['src/main/java/p/Foo.java',1]]) for t in members])
    monkeypatch.setattr(pools,'coverage',coverage)
    scope=dict(classes=[{'class':'p.Foo','file':'src/main/java/p/Foo.java'}])
    b=SimpleNamespace(exp=tmp_path,by_index={2:dict(date='date')},scope=scope,dev={'dev':dict(denominator='coverage-scoped')})
    write_json(tmp_path/'state/run.json',dict(status='running',run_id='test'))
    pool=pools.build_pool(b,dict(group='dev',index=2,sha='next'),2)
    assert {t['id'] for t in pool}=={'p.One#works','p.Two#works'}
    assert len(selected)==2 and all(t['declaring_file'].endswith('/p/Base.java') for t in pool)


def test_lexically_enclosed_base_takes_precedence_over_same_package_type():
    inv=inventory()
    inv['classes']['p.Outer$Base']=dict(kind='CLASS',package='p',modifiers='abstract',path='dev/src-test-t/java/p/Outer.java')
    inv['classes']['p.Outer$Child']=dict(kind='CLASS',package='p',extends='Base',path='dev/src-test-t/java/p/Outer.java')
    inv['methods'].append(dict(**{'class':'p.Outer$Base'},method='nestedTest',kind='plain',signature='nestedTest()',modifiers='protected'))
    recovered=[m for m in expand(inv)['methods'] if m['class']=='p.Outer$Child']
    assert [m['method'] for m in recovered]==['nestedTest']
    assert recovered[0]['declaring_class']=='p.Outer$Base'


def test_junit4_unannotated_override_keeps_inherited_test():
    inv=inventory()
    result=expand(inv,framework='junit4')
    inherited=[m for m in result['methods'] if m['class']=='p.One' and m['method']=='publicTest' and m['kind']]
    assert len(inherited)==1 and inherited[0]['declaring_class']=='p.Base'
