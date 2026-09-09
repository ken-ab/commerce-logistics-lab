"""Isolated lifecycle regressions: no model clients, business writes or network."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import site
import subprocess
import sys
from threading import Event
import time

import pytest

from apparel_fulfillment.jobs import AgentJobs
from apparel_fulfillment.orders import OrderError


class Drafts:
    def view(self, owner, draft_id):
        assert (owner, draft_id) == ('owner', 'draft')
        return {}


def result():
    return {'run_id': 'isolated', 'run_status': 'completed', 'report': None,
            'model_calls': 0, 'tool_calls': 0, 'input_tokens': 0, 'output_tokens': 0,
            'accounted_and_reserved_cny': '0', 'latency_seconds': 0,
            'delegations': 0, 'error_type': None, 'traces': []}


class ImmediateRunner:
    def __init__(self, *args, **kwargs): pass
    def run(self, task): return result()


@contextmanager
def live_service(directory, *, crash=False):
    script = '''
import json, site, sys
from pathlib import Path
from threading import Event
for directory in json.loads(sys.argv[2]):
    site.addsitedir(directory)
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_apparel_job_ownership import AgentJobs, Drafts, result
entered, release = Event(), Event()
class Runner:
    def __init__(self, *args, **kwargs): pass
    def run(self, task):
        entered.set()
        assert release.wait(30)
        return result()
directory = Path(sys.argv[1])
jobs = AgentJobs(Drafts(), directory=directory, agent_factory=Runner)
job = jobs.start('owner', 'draft', 'Isolated lifecycle check', 'single')
assert entered.wait(3)
temporary = directory / 'ready.tmp'
temporary.write_text(json.dumps(job), encoding='utf-8')
temporary.replace(directory / 'ready.json')
sys.stdin.readline()
release.set()
jobs.pool.shutdown(wait=True)
'''
    # Windows venv python.exe is a redirector; use its actual interpreter so Popen owns the worker PID.
    executable = getattr(sys, '_base_executable', sys.executable) if sys.platform == 'win32' else sys.executable
    process = subprocess.Popen([executable, '-u', '-c', script, str(directory), json.dumps(site.getsitepackages())],
                               cwd=Path(__file__).resolve().parents[1],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8')
    try:
        ready = directory / 'ready.json'
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), 'Isolated worker did not become ready'
        assert process.poll() is None
        yield process, json.loads(ready.read_text(encoding='utf-8'))
    finally:
        if crash and process.poll() is None:
            process.kill()
        try:
            _, errors = process.communicate(input='\n', timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            _, errors = process.communicate(timeout=5)
        if not crash:
            assert process.returncode == 0, errors


def test_reading_another_live_process_does_not_interrupt_its_job(tmp_path):
    directory = tmp_path / 'jobs'
    observer = AgentJobs(Drafts(), directory=directory, agent_factory=ImmediateRunner)
    try:
        with live_service(directory) as (process, job):
            before = observer.path(job['job_id']).read_bytes()
            assert observer.get('owner', job['job_id'])['status'] == 'running'
            assert observer.path(job['job_id']).read_bytes() == before
            with pytest.raises(OrderError, match='already active'):
                observer.start('owner', 'draft', 'No parallel work', 'single')
            assert len(list(directory.glob('AJOB-*.json'))) == 1
            assert process.poll() is None
        assert observer.get('owner', job['job_id'])['status'] == 'completed'
    finally:
        observer.pool.shutdown(wait=True)


def test_two_managers_sharing_a_directory_cannot_run_concurrently(tmp_path):
    entered, release = Event(), Event()
    class Runner:
        def __init__(self, *args, **kwargs): pass
        def run(self, task):
            entered.set()
            assert release.wait(5)
            return result()
    first = AgentJobs(Drafts(), directory=tmp_path / 'jobs', agent_factory=Runner)
    second = AgentJobs(Drafts(), directory=tmp_path / 'jobs', agent_factory=ImmediateRunner)
    try:
        first.start('owner', 'draft', 'First task', 'single')
        assert entered.wait(2)
        with pytest.raises(OrderError, match='already active'):
            second.start('owner', 'draft', 'Second task', 'single')
    finally:
        release.set()
        first.pool.shutdown(wait=True)
        second.pool.shutdown(wait=True)


def test_crashed_process_releases_lock_and_preserves_interrupted_record(tmp_path):
    directory = tmp_path / 'jobs'
    observer = AgentJobs(Drafts(), directory=directory, agent_factory=ImmediateRunner)
    try:
        with live_service(directory, crash=True) as (process, job):
            before = observer.get('owner', job['job_id'], full=True)
            assert before['process_id'] == process.pid and before['status'] == 'running'
        recovered = observer.get('owner', job['job_id'], full=True)
        assert recovered['status'] == 'interrupted'
        assert all(recovered[key] == value for key, value in before.items() if key != 'status')
        assert 'No automatic retry' in recovered['notice']
        assert len(list(directory.glob('AJOB-*.json'))) == 1
        next_job = observer.start('owner', 'draft', 'Explicit next task', 'single')
    finally:
        observer.pool.shutdown(wait=True)
    assert observer.get('owner', next_job['job_id'])['status'] == 'completed'


@pytest.mark.parametrize('previous,current,recover', [
    ('same', 'same', False), ('same', 'unknown', False),
    ('old', 'new-process', True), ('old', None, True),
    (None, 'live', False), (None, None, True),
    ('unknown', 'live', False), ('unknown', None, True),
])
def test_legacy_identity_recovery_is_conservative(tmp_path, previous, current, recover):
    jobs = AgentJobs(Drafts(), directory=tmp_path / 'jobs', agent_factory=ImmediateRunner,
                     identity_lookup=lambda pid: current)
    record = {'job_id': 'AJOB-' + 'a' * 32, 'owner': 'owner', 'draft_id': 'draft',
              'process_id': 12345, 'process_identity': previous, 'status': 'running',
              'arm': 'single', 'task': 'Legacy task', 'checkpoint': ['preserve-this']}
    jobs.save(record)
    before = jobs.path(record['job_id']).read_bytes()
    try:
        observed = jobs.get('owner', record['job_id'], full=True)
        assert observed['status'] == ('interrupted' if recover else 'running')
        assert observed['checkpoint'] == ['preserve-this']
        if not recover:
            assert jobs.path(record['job_id']).read_bytes() == before
            with pytest.raises(OrderError, match='legacy owner'):
                jobs.start('owner', 'draft', 'Must not overlap unknown work', 'single')
        else:
            jobs.start('owner', 'draft', 'Explicit follow-up', 'single')
    finally:
        jobs.pool.shutdown(wait=True)


def test_a_real_live_legacy_owner_is_preserved_until_it_exits(tmp_path):
    observer = AgentJobs(Drafts(), directory=tmp_path / 'legacy', agent_factory=ImmediateRunner)
    try:
        with live_service(tmp_path / 'child') as (_, job):
            original = json.loads((tmp_path / 'child' / (job['job_id'] + '.json')).read_text(encoding='utf-8'))
            original.pop('ownership', None)
            original.pop('process_identity', None)
            observer.save(original)
            assert observer.get('owner', job['job_id'])['status'] == 'running'
            with pytest.raises(OrderError, match='legacy owner'):
                observer.start('owner', 'draft', 'Wait for legacy owner', 'single')
        assert observer.get('owner', job['job_id'])['status'] == 'interrupted'
    finally:
        observer.pool.shutdown(wait=True)


def test_submission_failure_is_terminal_and_does_not_wedge_the_directory(tmp_path):
    jobs = AgentJobs(Drafts(), directory=tmp_path / 'jobs', agent_factory=ImmediateRunner)
    jobs.pool.shutdown(wait=True)
    with pytest.raises(RuntimeError, match='shutdown'):
        jobs.start('owner', 'draft', 'Cannot submit this', 'single')
    assert jobs.active is None
    failed = json.loads(next(jobs.directory.glob('AJOB-*.json')).read_text(encoding='utf-8'))
    assert failed['status'] == 'failed' and failed['error_type'] == 'RuntimeError'
    followup = AgentJobs(Drafts(), directory=jobs.directory, agent_factory=ImmediateRunner)
    try:
        new = followup.start('owner', 'draft', 'Explicit follow-up', 'single')
    finally:
        followup.pool.shutdown(wait=True)
    assert followup.get('owner', new['job_id'])['status'] == 'completed'


def test_initial_persistence_failure_releases_lock_without_running(tmp_path, monkeypatch):
    executions = []
    class Runner(ImmediateRunner):
        def run(self, task):
            executions.append(task)
            return result()
    jobs = AgentJobs(Drafts(), directory=tmp_path / 'jobs', agent_factory=Runner)
    followup = AgentJobs(Drafts(), directory=jobs.directory, agent_factory=Runner)
    def fail(record): raise OSError('isolated persistence failure')
    monkeypatch.setattr(jobs, 'save', fail)
    try:
        with pytest.raises(OSError):
            jobs.start('owner', 'draft', 'Not submitted', 'single')
        assert jobs.active is None and executions == []
        assert not list(jobs.directory.glob('AJOB-*.json'))
        followup.start('owner', 'draft', 'Explicit follow-up', 'single')
    finally:
        jobs.pool.shutdown(wait=True)
        followup.pool.shutdown(wait=True)
    assert executions == ['Explicit follow-up']


def test_terminal_persistence_failure_does_not_leave_a_live_pid_stuck(tmp_path, monkeypatch):
    executions = []
    class Runner(ImmediateRunner):
        def run(self, task):
            executions.append(task)
            return result()
    jobs = AgentJobs(Drafts(), directory=tmp_path / 'jobs', agent_factory=Runner)
    original_save = jobs.save
    def fail_terminal(record):
        if record['status'] == 'completed':
            raise OSError('isolated final write failure')
        original_save(record)
    monkeypatch.setattr(jobs, 'save', fail_terminal)
    try:
        job = jobs.start('owner', 'draft', 'One execution only', 'single')
    finally:
        jobs.pool.shutdown(wait=True)
    assert jobs.active is None and executions == ['One execution only']
    observer = AgentJobs(Drafts(), directory=jobs.directory, agent_factory=ImmediateRunner)
    try:
        record = observer.get('owner', job['job_id'], full=True)
        assert record['process_id'] == os.getpid() and record['status'] == 'interrupted'
        assert executions == ['One execution only']
        observer.start('owner', 'draft', 'Explicit follow-up', 'single')
    finally:
        observer.pool.shutdown(wait=True)
