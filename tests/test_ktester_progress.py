"""The optional observer must preserve the author's method behavior."""
from functools import partial
import importlib
import sys
from types import SimpleNamespace

import pytest

from test_survival.io import read_json


@pytest.fixture
def worker(monkeypatch):
    from test_survival.adapters import java_worker
    monkeypatch.setitem(sys.modules, 'java_worker', java_worker)
    monkeypatch.setattr(sys, 'dont_write_bytecode', sys.dont_write_bytecode)
    return importlib.import_module('test_survival.adapters.ktester_worker')


@pytest.mark.parametrize('author', [SimpleNamespace(), SimpleNamespace(Post=SimpleNamespace()),
                                     SimpleNamespace(Post=SimpleNamespace(CodeRepairer=type('Other', (), {})))])
def test_missing_optional_hook_is_harmless(worker, tmp_path, author):
    worker.observe_repairs(author, tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('failing_write', [1, 2, None])
def test_marker_failures_preserve_repair_call_and_return(worker, tmp_path, monkeypatch, failing_write):
    calls, writes, timings = [], [], []
    # Mirrors the descriptor protocol used by the author's TimeRecorder.
    class Timed:
        def __init__(self, fn):
            self.fn = fn
        def __get__(self, instance, owner):
            return self if instance is None else partial(self, instance)
        def __call__(self, *args, **kwargs):
            timings.append(args[1]['id'])
            return self.fn(*args, **kwargs)
    class Repairer:
        @Timed
        def check_test_class(self, task_info, *args, **kwargs):
            calls.append((task_info, args, kwargs))
            return 'repaired'
    original_write = worker.write_json
    def write(path, value):
        writes.append(value['status'])
        if len(writes) == failing_write:
            raise OSError('disk unavailable')
        original_write(path, value)
    monkeypatch.setattr(worker, 'write_json', write)
    worker.observe_repairs(SimpleNamespace(Post=SimpleNamespace(CodeRepairer=Repairer)), tmp_path)
    task = {'id': 'one', 'test-class': 'p.One_Test'}
    assert Repairer().check_test_class(task, 'prompt', response='response') == 'repaired'
    assert calls == [(task, ('prompt',), {'response': 'response'})]
    assert timings == ['one']
    assert writes == ['running', 'completed']
    if failing_write is None:
        assert read_json(tmp_path/'progress/one.json')['status'] == 'completed'


def test_failed_marker_does_not_mask_author_exception(worker, tmp_path, monkeypatch):
    error = RuntimeError('author repair failed')
    class Repairer:
        def check_test_class(self, task_info):
            raise error
    def unwritable(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(worker, 'write_json', unwritable)
    worker.observe_repairs(SimpleNamespace(Post=SimpleNamespace(CodeRepairer=Repairer)), tmp_path)
    with pytest.raises(RuntimeError) as caught:
        Repairer().check_test_class({'id': 'one', 'test-class': 'p.One_Test'})
    assert caught.value is error
