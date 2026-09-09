from copy import deepcopy
import hashlib

import pytest

from research.apparel_context_diagnostic import available_gap_fields, compact, context_measurements, repeated_reads, stock_review_flags


def tool(ident, sku='SKU-A', stock=8, role='product', **extra):
    return {'kind': 'tool', 'role': role, 'observation_id': ident, 'tool': 'read_variant', 'success': True,
            'arguments': {'sku': sku}, 'result': {'variant': {'sku': sku}, 'stock': {'available': stock}}, **extra}


def model(messages):
    return {'kind': 'model', 'role': 'coordinator', 'messages': deepcopy(messages),
            'input_characters': len(compact(messages)),
            'input_sha256': hashlib.sha256(compact(messages, sort_keys=True).encode()).hexdigest()}


def test_same_sku_changed_inventory_and_different_sku_are_not_deduplicated():
    traces = [tool('O-1'), tool('O-2', sku='SKU-B'), tool('O-3', stock=7),
              tool('O-4', stock=8), tool('O-5', stock=8, role='logistics')]
    count, repeats = repeated_reads(traces)
    assert count == 5
    assert len(repeats) == 1
    assert repeats[0]['previous_observation'] == 'O-4'
    assert repeats[0]['cross_role'] is True


def test_repeated_messages_count_transmission_but_handoff_only_once():
    payload = {'expert_report': {'answer': '库存8件，来源O-1', 'decision': {'status': 'ready'},
                                 'source_facts': [{'value': 8}]},
               'new_observations': [tool('O-1')], 'shared_calls_remaining': 4}
    messages = [{'role': 'tool', 'tool_call_id': 't1', 'content': compact(payload)}]
    trace = model(messages)
    before = deepcopy(trace)
    context, _, visible = context_measurements([trace, trace], 'coordinator')
    assert context['handoff_message_appearances'] == 2
    assert context['logical_handoffs_visible_to_root'] == 1
    stripped = deepcopy(payload); del stripped['expert_report']['answer']
    reduced = [{'role': 'tool', 'tool_call_id': 't1', 'content': compact(stripped)}]
    assert context['rendered_answer_removal_characters'] == 2 * (len(compact(messages)) - len(compact(reduced)))
    assert visible['O-1']['result']['stock']['available'] == 8
    assert trace == before  # Counterfactual measurement cannot change frozen evidence.


def test_corrupted_message_digest_fails_instead_of_silently_counting():
    trace = model([{'role': 'user', 'content': 'order'}])
    trace['messages'][0]['content'] = 'other'
    with pytest.raises(ValueError, match='digest'):
        context_measurements([trace], 'coordinator')


def test_old_proposal_same_arrival_is_not_evidence_for_current_version():
    run = {'report': {'decision': {'proposal_id': 'new'}},
           'after': {'proposals': [{'proposal_id': 'new', 'route': {'arrival_at': '2026-10-12'}}],
                     'order_check': {'status': 'ready'}}}
    score = {'evidence_gaps': ['current_route_arrival_at_not_cited']}
    obs = {'O-1': {'success': True, 'result': {'proposal': {'proposal_id': 'old', 'route': {'arrival_at': '2026-10-12'}}}}}
    assert not available_gap_fields(run, score, obs)[0]['available_in_last_root_input']
    obs['O-1']['result']['proposal']['proposal_id'] = 'new'
    fields = available_gap_fields(run, score, obs)[0]['observed_fields']
    assert fields == [{'observation_id': 'O-1', 'pointer': '/result/proposal/route/arrival_at', 'value': '2026-10-12'}]


def test_stock_source_binding_produces_review_flag_without_rescoring():
    run = {'report': {'decision': {'product_skus': ['SKU-B']},
                      'source_facts': [{'observation_id': 'O-1', 'pointer': '/result/stock/available'}]},
           'observations': {'O-1': tool('O-1')}}
    before = deepcopy(run)
    flags = stock_review_flags(run)
    assert flags[0]['source_sku'] == 'SKU-A'
    assert 'Review only' in flags[0]['notice']
    assert run == before
