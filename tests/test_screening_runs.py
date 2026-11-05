from types import SimpleNamespace

import pytest

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import validate
from test_survival.io import read_json, write_csv
from test_survival.policy.pools import pool_config
from test_survival.stages.screen import screen_round, normalize_funnel


@pytest.mark.parametrize('runs',[1,3,5])
def test_generator_pool_screening_and_funnel_follow_config(tmp_path,monkeypatch,runs):
    config=validate(dict(name='screen',anchor='t',project={'repo':str(tmp_path)},screen={'stable_runs':runs},dev={'stable_runs':runs+1},
                         generators=[dict(name='o',method='ouedraogo',impl='native')]))
    effective=pool_config(SimpleNamespace(execution_config=config),dict(group='o',sha='point'))
    assert effective['screen']['stable_runs']==runs and effective['dev']['stable_runs']==runs+1
    directory=tmp_path/'generated/o/round1';source=directory/'sources/A/ZSL/T.java';source.parent.mkdir(parents=True);source.write_text('class T {}')
    write_csv(tmp_path/'dev/dev-denominator.csv',[],['class','method','kind'])
    def screen(self,group,round_name,path,repeats,number):
        assert repeats==runs
        return dict(file=str(path.relative_to(tmp_path)),compile={'exit_code':0},methods=[dict(state='pass',body_hash='hash')])
    monkeypatch.setattr(JavaMavenAdapter,'screen_file',screen)
    _,funnel=screen_round(tmp_path,effective,effective['generators'][0],directory)
    assert funnel['stable_runs']==runs and funnel['stable_passed']==1
    assert 'stable_3_of_3' not in read_json(directory/'funnel.json')


def test_old_funnel_read_preserves_source_and_explicit_new_counts():
    old={'stable_3_of_3':12,'kept':10};before=dict(old)
    assert normalize_funnel(old)=={**old,'stable_runs':3,'stable_passed':12}
    assert old==before
    assert normalize_funnel({**old,'stable_runs':1,'stable_passed':8})['stable_passed']==8
