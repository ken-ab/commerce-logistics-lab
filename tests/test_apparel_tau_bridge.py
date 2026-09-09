"""Counterexamples for the offline bridge, never new model experiments."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest

from evaluation.apparel_tau_bridge import (NoModel, canonical_target, clone_initial, evaluate_record,
    executed_steps, no_scorer, read, sealed_connection, snapshot)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evidence/apparel_reliability_study_v1'
CASES = {c['id']: c for c in read(DATA / 'cases.json')}


def inputs(ident='RL-00-0', condition='v6_single'):
    folder = DATA / 'runs' / (ident + '-' + condition)
    return CASES[ident], read(folder / 'execution.json'), DATA / 'initial' / ident, folder / 'operations.sqlite'


def evaluate(tmp_path, args, version='v6'):
    return evaluate_record(*args, tmp_path / 'replay', version)


def test_real_trace_replays_with_all_state_and_fresh_uuid_binding(tmp_path):
    result = evaluate(tmp_path, inputs())
    assert result['replay_fidelity_passed'] and result['target_db_match']
    assert result['recorded_tools'] == result['replayed_tools']
    states = [read(tmp_path / 'replay' / (k + '_state.json')) for k in ('predicted', 'live', 'reference')]
    assert states[0] == states[1]
    assert states[0] != states[2]  # Golden creation uses an independent UUID.
    assert canonical_target(states[0]) == canonical_target(states[2])


def test_real_business_error_is_reexecuted_not_skipped(tmp_path):
    result = evaluate(tmp_path, inputs('RL-02-0', 'v5_on_demand'), 'v5')
    assert result['recorded_business_errors'] == 1
    assert result['replayed_tools'] == result['recorded_tools']
    assert result['replay_fidelity_passed'] and result['target_db_match']


@pytest.mark.parametrize('tool', ['read_variant', 'prepare_proposal'])
def test_upstream_strict_mode_catches_fabricated_read_and_write_outputs(tmp_path, tool):
    args = list(inputs())
    step = next(t for t in args[1]['traces'] if t['kind'] == 'tool' and t['tool'] == tool)
    step['result']['invented_fact'] = 'This value did not come from the tool'
    result = evaluate(tmp_path, args)
    assert not result['strict_replay_passed'] and not result['replay_fidelity_passed']
    assert result['replay_error']['type'] == 'ValueError'


def test_wrong_proposal_id_cannot_alias_the_current_order(tmp_path):
    args = list(inputs())
    other = read(DATA / 'initial/RL-00-1/seed.json')['old_id']
    step = next(t for t in args[1]['traces'] if t['kind'] == 'tool' and t['tool'] == 'read_proposal')
    step['arguments']['proposal_id'] = other
    result = evaluate(tmp_path, args)
    assert not result['strict_replay_passed']


def test_unlogged_inventory_change_outside_selected_sku_is_detected(tmp_path):
    args = list(inputs())
    altered = tmp_path / 'unlogged.sqlite'
    with closing(sealed_connection(args[3])) as source, closing(sqlite3.connect(altered)) as target:
        source.backup(target)
        selected = {s['sku'] for s in args[1]['after']['selections']}
        sku = next(r[0] for r in target.execute('SELECT sku FROM inventory') if r[0] not in selected)
        target.execute('UPDATE inventory SET quantity=quantity-1 WHERE sku=?', (sku,))
        target.commit()
    args[3] = altered
    result = evaluate(tmp_path, args)
    assert result['strict_replay_passed'] and result['target_db_match']
    assert not result['live_state_match'] and not result['replay_fidelity_passed']


def test_omitted_write_cannot_reach_registered_target(tmp_path):
    args = list(inputs())
    x = args[1]
    stop = next(i for i, t in enumerate(x['traces']) if t['kind'] == 'tool' and t['tool'] == 'prepare_proposal')
    x['traces'] = x['traces'][:stop]
    steps = [t for t in x['traces'] if t['kind'] == 'tool']
    x['tool_calls'] = len(steps)
    x['successful_tool_calls'] = sum(t['success'] for t in steps)
    result = evaluate(tmp_path, args)
    assert result['strict_replay_passed'] and result['exact_output_types_passed']
    assert not result['target_db_match'] and not result['live_state_match']


def test_extra_valid_read_does_not_require_identical_reference_sequence(tmp_path):
    args = list(inputs())
    x = args[1]
    step = deepcopy(next(t for t in x['traces'] if t['kind'] == 'tool' and t['tool'] == 'read_variant'))
    step['observation_id'] = 'O-' + str(x['tool_calls'] + 1)
    x['traces'].append(step)
    x['tool_calls'] += 1
    x['successful_tool_calls'] += 1
    result = evaluate(tmp_path, args)
    assert result['replay_fidelity_passed'] and result['target_db_match']


def test_generated_identity_cannot_reuse_an_existing_proposal(tmp_path):
    args = list(inputs())
    step = next(t for t in args[1]['traces'] if t['kind'] == 'tool' and t['tool'] == 'prepare_proposal')
    step['result']['proposal_id'] = args[1]['before']['proposals'][-1]['proposal_id']
    result = evaluate(tmp_path, args)
    assert not result['strict_replay_passed']


def test_version_relationships_survive_canonicalization():
    state = snapshot(inputs()[3])
    wrong_previous = deepcopy(state)
    latest = max(wrong_previous['proposals'], key=lambda p: p['version'])
    latest['payload']['previous_proposal_id'] = latest['id']
    assert canonical_target(state) != canonical_target(wrong_previous)
    wrong_order = deepcopy(state)
    wrong_order['proposals'][-1]['draft_id'] = 'APP-some-other-order'
    assert canonical_target(state) != canonical_target(wrong_order)


def test_numeric_string_changes_are_detected_even_if_upstream_serialization_accepts(tmp_path):
    args = list(inputs())
    step = next(t for t in args[1]['traces'] if t['kind'] == 'tool' and t['tool'] == 'read_order')
    step['result']['revision'] = str(step['result']['revision'])
    result = evaluate(tmp_path, args)
    assert result['strict_replay_passed']  # Upstream serializes primitive numbers to strings.
    assert not result['exact_output_types_passed'] and not result['replay_fidelity_passed']


def test_unknown_tools_and_boolean_type_changes_fail_closed():
    x = inputs()[1]
    step = next(t for t in x['traces'] if t['kind'] == 'tool')
    step['tool'] = 'approve_substitution'
    with pytest.raises(ValueError, match='Unknown executed'):
        executed_steps(x)
    x = inputs()[1]
    next(t for t in x['traces'] if t['kind'] == 'tool')['success'] = 1
    with pytest.raises(ValueError):
        executed_steps(x)


def test_model_and_gpu_clients_are_unavailable():
    with pytest.raises(AssertionError, match='Model calls'):
        NoModel().chat([])
    with pytest.raises(AssertionError, match='GPU/network'):
        no_scorer('query', [])
