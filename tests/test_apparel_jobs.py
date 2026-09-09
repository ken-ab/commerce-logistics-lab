from threading import Event

import pytest

from apparel_fulfillment.jobs import AgentJobs
from apparel_fulfillment.orders import OrderError
from apparel_fulfillment.store import ApparelStore
from test_apparel_orders import fixture, request


def test_interactive_job_lock_ownership_persistence_and_full_records(tmp_path):
    store = ApparelStore(tmp_path / 'operations.sqlite', world=fixture())
    draft = store.create_draft('owner', request())
    entered, release, completed = Event(), Event(), Event()
    class Runner:
        def __init__(self, *args, **kwargs): pass
        def run(self, task):
            entered.set()
            assert release.wait(5)
            return {'run_id': 'fixture', 'run_status': 'completed', 'report': None, 'model_calls': 0, 'tool_calls': 0,
                    'input_tokens': 0, 'output_tokens': 0, 'accounted_and_reserved_cny': '0', 'latency_seconds': 0,
                    'delegations': 0, 'error_type': None, 'traces': [{'kind': 'fixture'}]}
    jobs = AgentJobs(store, directory=tmp_path / 'jobs', agent_factory=Runner)
    try:
        job = jobs.start('owner', draft['id'], 'Read this order.', 'single')
        assert entered.wait(2)
        assert jobs.get('owner', job['job_id'])['status'] == 'running'
        with pytest.raises(OrderError, match='already active'): jobs.start('owner', draft['id'], 'Duplicate task', 'single')
        with pytest.raises(OrderError, match='this session'): jobs.get('other', job['job_id'])
        with pytest.raises(OrderError): jobs.get('owner', '../anything')
    finally:
        release.set()
        jobs.pool.shutdown(wait=True)
    result = jobs.get('owner', job['job_id'])
    assert result['status'] == 'completed' and result['result']['model_calls'] == 0
    assert 'traces' not in result['result']
    assert jobs.get('owner', job['job_id'], full=True)['result']['traces'] == [{'kind': 'fixture'}]
    restored = AgentJobs(store, directory=tmp_path / 'jobs', agent_factory=Runner)
    try: assert restored.get('owner', job['job_id'])['status'] == 'completed'
    finally: restored.pool.shutdown(wait=True)
