from copy import deepcopy
from types import SimpleNamespace

import pytest

from apparel_fulfillment.data import digest
from research.apparel_state_recover_v3 import recover_result


def example():
    before = {'selections': [], 'revision': 1}
    case = {'id': 'test', 'scenario': 'example', 'family': 'order_ready', 'target_sku': 'A', 'contract': {'mode': 'prepare_proposal'}}
    attempt = {'run_id': 'run', 'registration_sha256': 'registration'}
    seed = {'draft_id': 'draft', 'initial_view_digest': digest(before)}
    traces = [{'kind': 'start', 'at': '2026-09-08T10:00:00Z', 'initial_state': before},
              {'kind': 'model', 'at': '2026-09-08T10:00:01Z', 'response': {'budget_call_id': 'known', 'role': 'single',
                  'status': 'success', 'usage': {'prompt_tokens': 10, 'completion_tokens': 3},
                  'estimated_cost_cny': '0.000030', 'latency_seconds': 1, 'message': {'content': None}}},
              {'kind': 'tool', 'at': '2026-09-08T10:00:01Z', 'role': 'single', 'observation_id': 'O-1',
               'tool': 'read_order', 'success': True, 'result': {'order_check': {'status': 'needs_clarification'}}},
              {'kind': 'citation_directory', 'at': '2026-09-08T10:00:01Z', 'observation_id': 'O-1', 'directory': {'paths': ['/result/order_check']}}]
    ledger = [{'id': 'known', 'model': 'gpt-5.6-luna', 'created_at': '2026-09-08T10:00:00Z', 'charged': 30, 'reserved': 80, 'usage': None},
              {'id': 'unknown', 'model': 'gpt-5.6-luna', 'created_at': '2026-09-08T10:00:02Z', 'charged': None, 'reserved': 75, 'usage': None}]
    store = SimpleNamespace(view=lambda owner, draft: deepcopy(before))
    return case, 'guard', 'single', attempt, seed, store, traces, ledger, 'manifest'


def test_interruption_preserves_known_trace_full_reservation_and_unknown_time():
    r = recover_result(*example())
    assert r['report'] is None and r['run_status'] == 'interrupted'
    assert r['successful_model_calls'] == 1 and r['model_calls'] == 2
    assert r['input_tokens'] == 10 and r['output_tokens'] == 3
    assert r['accounted_and_reserved_cny'] == '0.000105'
    assert r['calls'][-1]['status'] == 'interrupted_unknown' and r['calls'][-1]['usage'] is None
    assert r['calls'][-1]['latency_seconds'] is None
    assert r['latency_observation'] == 'lower_bound_censored' and r['latency_seconds'] == 2
    assert r['observations']['O-1']['citation_directory']['paths'] == ['/result/order_check']


def test_recovery_refuses_to_relabel_completed_or_unmatched_trace():
    args = list(example()); args[6].append({'kind': 'completed'})
    with pytest.raises(ValueError, match='completed marker'): recover_result(*args)
    args = list(example()); args[7] = args[7][1:]
    with pytest.raises(ValueError, match='no matching ledger'): recover_result(*args)
