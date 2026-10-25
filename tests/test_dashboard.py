import json
import yaml
from test_survival.dashboard.build import build,per_group,testpilot_ss as read_testpilot


def test_legacy_column_mapping_and_coverage(tmp_path):
    p=tmp_path/'project/experiments/old';(p/'results').mkdir(parents=True)
    (p/'results/llm-survival.csv').write_text('index,sha,alive_count\n0,abc,10\n1,def,4\n')
    (p/'results/changed-lines.csv').write_text('all_executable_changed_lines,all_llm_covered\n5,3\n10,5\n')
    result=per_group(p)
    assert result['denominators']=={'llm':10}
    assert result['coverage']['llm']=={'covered':8,'executable':15}


def test_self_contained_safe_embedded_data(tmp_path):
    p=tmp_path/'project/experiments/test';(p/'results').mkdir(parents=True)
    (p/'results/survival-by-commit.csv').write_text('n,sha7,tags,llm_cum,dev_cum_subtests\n0,abc,v1,10,20\n1,def,,5,15\n')
    config=tmp_path/'experiments.yaml'
    config.write_text(yaml.safe_dump({'experiments':[{'path':str(p),'title':'</script><script>bad</script>','layout':'testpilot-ss'}]}))
    build(config,tmp_path/'out')
    html=(tmp_path/'out/index.html').read_text()
    assert '</script><script>bad' not in html
    assert '\\u003c/script' in html
    assert read_testpilot(p)['coverage']=={'dev':None,'llm':None}
    assert 'src="http' not in html and 'href="http' not in html
