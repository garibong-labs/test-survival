from copy import deepcopy
import hashlib

import pytest

from test_survival.adapters.java_maven import JavaMavenAdapter
from test_survival.config import as_written, canonical_hash, validate
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


def test_records_keyed_before_the_operational_hash_policy_are_reused(tmp_path, monkeypatch):
    config = validate(dict(name='resume', project={'repo': '/unused'}, anchor='t',
                           runner={'workers': {'build': 2}, 'reuse': {'checkouts': '/old/checkouts'},
                                   'docker': {'mounts': {'/host/repo': '/repo'}, 'env_file': '/private/docker.env'}}))
    method = {'file': 'T.java', 'class': 'T', 'method': 'test', 'id': 'one'}
    (tmp_path/'T.java').write_text('class T {}')
    write_json(tmp_path/'results/anchor.json', {'sha': 'anchor'})
    adapter = JavaMavenAdapter(tmp_path, config)
    provenance = adapter.provenance()
    # Keys exactly as main computed them before this policy: the full runner, operational settings included.
    legacy_runner = as_written(config['runner'], include_defaults=True)
    legacy_runner['docker'].pop('m2_volume')
    source = hashlib.sha256(b'class T {}').hexdigest()
    build_key = canonical_hash({'sha': 'anchor', 'runner': legacy_runner, 'module': '.'})
    run_key = canonical_hash({'methods': [('one', source)], 'project': config['project'], 'survival': config['survival'],
                              'runner': config['runner'], 'repeats': 1})
    coverage_key = canonical_hash({'method': method, 'source': source, 'project': config['project'], 'runner': config['runner'],
                                   'environment_errors': config['survival']['environment_errors']})
    assert build_key != adapter.build_fingerprint('anchor') and run_key != adapter.run_fingerprint([method])
    (adapter.folder(0)/'target/classes').mkdir(parents=True)
    write_json(tmp_path/'results/builds/0.json', {'sha': 'anchor', 'build': 'ok', 'input_sha256': build_key})
    write_json(tmp_path/'results/raw/0-dev.json', {'sha': 'anchor', 'input_sha256': run_key, 'runner_sha256': provenance})
    write_json(tmp_path/'results/dev-method-coverage-t/T#test.json', {'sha': 'anchor', 'input_sha256': coverage_key, 'runner_sha256': provenance})
    (tmp_path/'results/dev-method-coverage-t/T#test.xml').write_text('<coverage/>')
    monkeypatch.setattr(JavaMavenAdapter, 'worker', lambda self, job: pytest.fail('legacy-keyed record was remeasured'))
    assert adapter.build(0, 'anchor')['input_sha256'] == build_key
    assert adapter.run_group(0, 'dev', [method])['input_sha256'] == run_key
    assert adapter.method_coverage(method)['input_sha256'] == coverage_key
