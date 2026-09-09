import json

import pytest

from apparel_fulfillment.action_contract import TaskContract
from apparel_fulfillment.data import digest
from apparel_fulfillment.transport import iso
from research.apparel_state_cases_v3 import cases as development_cases, setup_case
from research.apparel_state_pilot_v3 import fixture_check
from research.apparel_state_replication_cases import cases


@pytest.mark.parametrize('case', cases(), ids=lambda c: c['scenario'])
def test_new_order_states_have_feasible_independent_expectations(tmp_path, case):
    assert case['id'] not in {c['id'] for c in development_cases()}
    assert case['request'] not in [c['request'] for c in development_cases()]
    TaskContract.model_validate(case['contract'])
    store, draft_id, now = setup_case(case, tmp_path / 'operations.sqlite')
    initial = store.view('evaluation', draft_id)
    assert 'SCREP-' not in json.dumps({'view': initial, 'world': store.base_world, 'events': store.transport_events()})
    seed = {'draft_id': draft_id, 'now': iso(now), 'initial_view_digest': digest(initial)}
    assert fixture_check(case, tmp_path, seed, store.base_world)['passed']
    assert digest(store.view('evaluation', draft_id)) == seed['initial_view_digest']
