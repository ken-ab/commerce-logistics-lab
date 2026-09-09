from copy import deepcopy

import pytest

from apparel_fulfillment.candidate_search import search_variants
from apparel_fulfillment.data import load_world, digest
from apparel_fulfillment.orders import check_order
from apparel_fulfillment.agent_candidate_v4 import CandidateSearchAgent
from apparel_fulfillment.store import ApparelStore


def scorer(query, rows):
    return [{'id': row['id'], 'score': (i + 1) / (len(rows) + 1)} for i, row in enumerate(rows)]


def test_scoring_never_relaxes_exact_filters_or_changes_original_fields():
    world = load_world()
    before = digest(world)
    args = {'query': 'cotton shirt nonexistentword', 'brand': 'Goodthreads', 'color': 'black', 'size': 'M'}
    result = search_variants(world, args, scorer=scorer)
    expected = {sku for sku, v in world['variants'].items() if
        (v['brand'], v['color'], v['size']) == ('Goodthreads', 'black', 'M')}
    assert {v['sku'] for v in result['variants']} == expected
    assert result['retrieval']['eligible_variants_before_ranking'] == len(expected)
    assert all(v['field_provenance'] == world['variants'][v['sku']]['provenance'] for v in result['variants'])
    assert digest(world) == before
    assert not search_variants(world, args, match_mode='all', scorer=scorer)['variants']


def test_invalid_ranker_cannot_insert_a_product_and_fallback_is_visible():
    result = search_variants(load_world(), {'query': 'cotton shirt', 'color': 'black'},
        scorer=lambda q, rows: [{'id': 'invented', 'score': 1}])
    assert result['variants'] and all(v['sku'] != 'invented' for v in result['variants'])
    assert result['retrieval']['method'] == 'fts_bm25_fallback'
    assert result['retrieval']['fallback_reason'] == 'local_reranker_unavailable_or_invalid'


def test_filters_precede_limit_and_empty_request_browses_without_a_model():
    world = load_world()
    target = sorted(world['variants'])[-1]
    v = world['variants'][target]
    args = {key: v[key] for key in ('brand', 'color', 'size', 'style_id')}
    def no_model(*_):
        raise AssertionError('An empty query should not call the ranker')
    result = search_variants(world, args, scorer=no_model, limit=1)
    assert result['variants'] and all(result['variants'][0][k] == args[k] for k in args)
    assert result['retrieval']['method'] == 'source_scoped_browse'
    assert not search_variants(world, {'query': target, 'brand': 'wrong'}, scorer=no_model)['variants']
    direct = search_variants(world, {'query': target}, scorer=no_model)
    assert direct['variants'][0]['sku'] == target
    assert direct['retrieval']['method'] == 'single_filtered_candidate'


def test_ranked_match_does_not_bypass_missing_unit_stock_or_substitution_check():
    world = load_world()
    v = world['variants']['us:B06XWGZD1C']
    result = search_variants(world, {'query': v['sku']}, scorer=scorer)
    assert result['variants'][0]['sku'] == v['sku']
    request = {'sales_region': 'DE', 'wholesale': False, 'needs_shipping': False,
        'lines': [{'line_id': 'one', 'quantity': 20, 'unit': None, 'brand': v['brand'],
                   'color': v['color'], 'size': 'M', 'category': 't_shirt'}]}
    checked = check_order(request, [{'line_id': 'one', 'sku': v['sku']}], world)
    codes = {i['code'] for i in checked['issues']}
    assert checked['status'] == 'needs_clarification'
    assert {'quantity_unit_unspecified', 'substitution_requires_confirmation'} <= codes
    request['lines'][0]['unit'] = 'piece'
    world['stock'][v['sku']]['available_catalog_units'] = 0
    assert check_order(request, [{'line_id': 'one', 'sku': v['sku']}], world)['status'] == 'unfulfillable'


def test_agent_adapter_search_is_observed_without_selection_or_approval(tmp_path):
    world = load_world()
    request = {'sales_region': 'DE', 'wholesale': False, 'needs_shipping': False,
        'lines': [{'line_id': 'one', 'quantity': 20, 'unit': 'piece', 'brand': 'Goodthreads',
                   'color': 'black', 'size': 'M', 'category': 't_shirt'}]}
    store = ApparelStore(tmp_path / 'search.sqlite', world=deepcopy(world))
    draft = store.create_draft('owner', request)
    agent = CandidateSearchAgent(store, 'owner', draft['id'], client=object(), candidate_scorer=scorer,
        contract={'mode': 'check_order'})
    result = agent.observed('search_variants', {'query': 'cotton shirt', 'size': 'M', 'color': 'black'}, 'single')
    assert result['success'] and result['result']['variants']
    after = store.view('owner', draft['id'])
    assert after['revision'] == draft['revision'] and after['selections'] == []
    assert after['approved_substitutions'] == [] and after['confirmation'] is None
    with pytest.raises(ValueError, match='read-only'):
        agent.invoke('select_variants', {'selections': [{'line_id': 'one', 'sku': 'us:B06XWGZD1C'}], 'expected_revision': draft['revision']})
