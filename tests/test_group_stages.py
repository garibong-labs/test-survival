from contextlib import nullcontext
from copy import deepcopy
from test_survival.config import validate
from test_survival.io import write_json,write_csv,read_json,read_csv
from test_survival.stages import survive,cover


def test_skipping_and_derived_survival_with_coverage_predecessor_alias(tmp_path,monkeypatch):
    config=validate({'name':'fixture','project':{'repo':str(tmp_path)},'anchor':'t','range':{'skip_untouched':'main-sources'},
                     'dev':{'groups':[{'name':'dev'},{'name':'dev-scope','derive_from':'dev','denominator':'coverage-scoped'}]},
                     'generators':[{'name':'o','method':'ouedraogo','impl':'native'}]})
    methods=[dict(id=m,file='T.java',**{'class':'T'},method=m,kind='plain') for m in ['a','b']]
    frozen={'dev':methods,'dev-scope':methods[1:],'o':[]}
    write_json(tmp_path/'results/frozen-denominators.json',frozen)
    write_json(tmp_path/'results/anchor.json',{'sha':'anchor','date':'date'})
    write_json(tmp_path/'results/commit-paths.json',{'1':['src/main/java/A.java'],'2':['README.md'],'3':['src/main/java/A.java']})
    commits=[dict(index=i,sha=str(i),date='date',subject='change',**{'changes-src-main':'no' if i==2 else 'yes','changes-pom':'no'}) for i in [1,2,3]]
    write_csv(tmp_path/'results/commits.csv',commits,list(commits[0]))
    for i in [1,2,3]:write_json(tmp_path/f'results/builds/{i}.json',{'build':'ok','sha':str(i)})
    write_json(tmp_path/'population/scope.json',{'classes':[{'file':'src/main/java/A.java'}]})
    calls=[];coverage_calls=[]
    class Adapter:
        def __init__(self,*args):self.runner=self
        def setup_runner(self):pass
        def session(self,stage):return nullcontext()
        def run_group(self,index,group,rows,*,dead_at=None):
            calls.append((index,group));rows=deepcopy(rows)
            for row in rows:row.update(state='fail' if index==3 and row['id']=='b' else 'pass',flaky=False,message='assertion')
            raw={'index':index,'sha':str(index),'group':group,'status':'evaluated','phase':'A','methods':rows,'requested_ids':[r['id'] for r in rows],'files':[{'file':'T.java','methods':rows,'compile':{'exit_code':0},'runs':[]}]}
            write_json(tmp_path/f'results/raw/{index}-{group}.json',raw);return raw
        def git(self,*args):return 'parent' if args[0]=='rev-parse' else '--- a/src/main/java/A.java\n+++ b/src/main/java/A.java\n@@ -1 +1 @@\n-old\n+new'
        def coverage(self,index,files,rows,group):
            coverage_calls.append((index,group))
            write_csv(tmp_path/f'results/coverage/{index}-{group}-lines.csv',[{'file':'src/main/java/A.java','line':1,'covered':'yes' if rows else 'no'}],['file','line','covered'])
            return {'status':'evaluated'}
    monkeypatch.setattr(survive,'JavaMavenAdapter',Adapter)
    monkeypatch.setattr(cover,'JavaMavenAdapter',Adapter)
    survive.run(tmp_path,config)
    assert sorted(calls)==[(1,'dev'),(1,'o'),(3,'dev'),(3,'o')]
    curves=read_csv(tmp_path/'results/survival.csv')
    assert curves[2]['evaluated']=='no' and curves[2]['dev_alive']=='2'
    assert curves[3]['dev_alive']=='1' and curves[3]['dev-scope_alive']=='0'
    assert read_json(tmp_path/'results/raw-derived/1-dev-scope.json')['derived_from']=='dev'
    assert read_json(tmp_path/'results/raw/2-dev.json')=={'index':2,'sha':'2','group':'dev','phase':'A','status':'skipped-untouched'}
    check=read_json(tmp_path/'results/survival-consistency.json')
    assert check['measured_raw_records']==6 and check['derived_raw_records']==3 and check['raw_count_matches']
    cover.run(tmp_path,config)
    assert {i for i,g in coverage_calls}=={0,1,3}
    assert len(coverage_calls)==9
    assert read_json(tmp_path/'results/coverage-index-aliases.json')=={'2':1}
    assert read_csv(tmp_path/'results/changed-lines.csv')[-1]['dev_pre_image_covered']=='1'
