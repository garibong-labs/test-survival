from copy import deepcopy

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import validate
from test_survival.io import write_json


def test_operational_changes_reuse_build_run_and_method_coverage(tmp_path, monkeypatch):
    config = validate(dict(name='resume', project={'repo': '/unused'}, anchor='t'))
    method = {'file': 'T.java', 'class': 'T', 'method': 'test', 'id': 'one'}
    (tmp_path/'T.java').write_text('class T {}')
    write_json(tmp_path/'results/anchor.json', {'sha': 'anchor'})
    initial = JavaMavenAdapter(tmp_path, config)
    (initial.folder(0)/'target/classes').mkdir(parents=True)
    write_json(tmp_path/'results/builds/0.json', {'sha': 'anchor', 'build': 'ok', 'input_sha256': initial.build_fingerprint('anchor')})
    calls = []
    def worker(self, job):
        calls.append(job)
        result = {'sha': job['sha'], 'input_sha256': job['input_sha256'], 'runner_sha256': job['runner_sha256']}
        write_json(self.exp/job['output'], result)
        if job.get('coverage_xml'):
            path = self.exp/job['coverage_xml']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('<coverage/>')
        return result
    monkeypatch.setattr(JavaMavenAdapter, 'worker', worker)
    initial.run_group(0, 'dev', [method])
    initial.method_coverage(method)
    assert len(calls) == 2
    changed = deepcopy(config)
    changed['runner']['workers'].update(build=1, survive=2, cover=3)
    changed['runner']['reuse'].update(checkouts='/other/checkouts', classpath_cache='/other/classpath')
    changed['runner']['docker'].update(mounts={'/different': '/repo'}, env_file='/private/docker.env')
    changed['model']['env_file'] = '/private/model.env'
    resumed = JavaMavenAdapter(tmp_path, changed)
    assert resumed.build_fingerprint('anchor') == initial.build_fingerprint('anchor')
    assert resumed.run_fingerprint([method]) == initial.run_fingerprint([method])
    resumed.build(0, 'anchor')
    resumed.run_group(0, 'dev', [method])
    resumed.method_coverage(method)
    assert len(calls) == 2
    changed['runner']['timeouts']['test_class'] += 1
    assert JavaMavenAdapter(tmp_path, changed).build_fingerprint('anchor') != initial.build_fingerprint('anchor')
    JavaMavenAdapter(tmp_path, changed).run_group(0, 'dev', [method])
    JavaMavenAdapter(tmp_path, changed).method_coverage(method)
    assert len(calls) == 4
