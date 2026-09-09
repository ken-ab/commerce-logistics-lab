"""One continuous order-to-confirmation acceptance story, with no model calls."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apparel_fulfillment.api import install_apparel
from apparel_fulfillment.store import ApparelStore
from commerce_lab.state import Store
from test_apparel_orders import fixture, request


NOW = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)


class NoModelJobs:
    def start(self, *args, **kwargs):
        raise AssertionError('This acceptance story must not start a model job')


def test_substitute_cancel_revise_stock_change_and_confirm_once(tmp_path, record_property):
    """A customer's approval survives route changes, but never waives stock checks."""
    world = fixture()
    world['notice'] = 'Isolated workflow acceptance fixture; all business data simulated'
    world['stock']['A']['available_catalog_units'] = 0
    for variant in world['variants'].values():
        variant['provenance']['weight_grams_per_catalog_unit'] = {'evidence_id': 'fixture-weight'}
    store = ApparelStore(tmp_path / 'orders.sqlite', world=world)
    sessions = Store(catalog=object(), path=tmp_path / 'sessions.sqlite')
    app = install_apparel(FastAPI(), operations=store, sessions=sessions, jobs=NoModelJobs())
    trace, milestones = [], []
    order = request()
    order.update(needs_shipping=True, shipping={'destination': 'DE-DC',
                 'ready_at': '2026-09-10T00:00:00Z', 'deadline_at': '2026-09-17T00:00:00Z',
                 'budget_cents': 20000})

    def stock():
        with closing(store.connect()) as db:
            return dict(db.execute("SELECT * FROM inventory WHERE sku='B'").fetchone())

    try:
        with patch('apparel_fulfillment.store.datetime') as clock, TestClient(app) as client:
            clock.now.return_value = NOW
            client.headers['X-Session-ID'] = sessions.session()['id']

            def call(method, path, payload=None, *, status=200, headers=None):
                response = client.request(method, path, json=payload, headers=headers)
                body = response.json()
                trace.append({'method': method, 'path': path, 'input': payload,
                              'status_code': response.status_code, 'response': body})
                assert response.status_code == status
                return body

            draft = call('POST', '/api/apparel/drafts', {'order': order})
            path = '/api/apparel/drafts/' + draft['id']
            draft = call('POST', path + '/select', {'selections': [{'line_id': 'one', 'sku': 'A'}],
                         'expected_revision': draft['revision']})
            assert draft['order_check']['status'] == 'unfulfillable'
            candidates = call('GET', path + '/alternatives/one')
            candidate = next(c for c in candidates if c['sku'] == 'B')
            assert candidate['requires_confirmation']
            assert candidate['differences'] == [{'field': 'brand', 'requested': 'BrandA', 'candidate': 'BrandB'}]
            milestones.append('Original SKU unavailable; alternative exposes its changed brand')

            draft = call('POST', path + '/select', {'selections': [{'line_id': 'one', 'sku': 'B'}],
                         'expected_revision': draft['revision']})
            assert draft['order_check']['status'] == 'needs_clarification'
            refusal = call('POST', path + '/proposals', {'expected_revision': draft['revision']})
            assert refusal['status'] == 'order_not_ready'
            call('POST', path + '/approve-substitution', {'approval_id': 'not-this-change',
                 'expected_revision': draft['revision']}, status=409)
            draft = call('POST', path + '/approve-substitution', {'approval_id': candidate['approval_id'],
                         'expected_revision': draft['revision']})
            assert draft['order_check']['status'] == 'ready'
            assert stock()['quantity'] == 20
            milestones.append('Only the exact approved alternative permits planning; no stock deducted yet')

            first = call('POST', path + '/proposals', {'expected_revision': draft['revision']})
            assert first['state'] == 'pending'
            assert first['route']['shipping_constraints'] == order['shipping']
            initial_route = deepcopy(first['route'])
            flight = next(s for s in first['route']['segments'] if s['mode'] == 'air')
            event = {'event_id': 'workflow-cancel', 'kind': 'cancel', 'leg_id': flight['leg_id'],
                     'nominal_departure': flight['nominal_departure'], 'published_at': NOW.isoformat()}
            call('POST', '/api/apparel/events', event)
            old = path + '/proposals/' + first['proposal_id']
            assert not call('GET', old + '/validity')['valid']
            assert call('POST', old + '/confirm', {})['status'] == 'rejected'
            assert stock()['quantity'] == 20
            milestones.append('Cancellation invalidates the old proposal; rejected confirmation does not deduct stock')

            second = call('POST', path + '/proposals', {'expected_revision': draft['revision']})
            assert second['version'] == 2 and second['previous_proposal_id'] == first['proposal_id']
            assert second['order_check']['quantities_catalog_units'] == {'B': 20}
            assert second['route']['shipping_constraints'] == order['shipping']
            assert second['independent_route_audit']['passed']
            revised = path + '/proposals/' + second['proposal_id']
            store.set_simulated_stock('B', 19, expected_version=1)
            refused = call('POST', revised + '/confirm', {})
            assert refused['status'] == 'rejected'
            assert 'inventory_or_rule_snapshot_changed' in refused['assessment']['violations']
            assert stock()['quantity'] == 19
            milestones.append('Revised transport preserves the approved goods and constraints; later stock loss still blocks confirmation')

            store.set_simulated_stock('B', 20, expected_version=2)
            third = call('POST', path + '/proposals', {'expected_revision': draft['revision']})
            assert third['version'] == 3 and third['previous_proposal_id'] == second['proposal_id']
            assert third['route']['shipping_constraints'] == order['shipping']
            assert third['independent_route_audit']['passed']
            final = path + '/proposals/' + third['proposal_id']
            confirmed = call('POST', final + '/confirm', {})
            repeated = call('POST', final + '/confirm', {})
            assert confirmed['status'] == 'confirmed_simulation'
            assert repeated['idempotent_replay']
            assert repeated['confirmation_id'] == confirmed['confirmation_id']
            assert stock()['quantity'] == 0
            with closing(store.connect()) as db:
                assert db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM traces WHERE kind='confirm_simulation'").fetchone()[0] == 1
            milestones.append('Fresh proposal confirms once; repeated request returns the same confirmation with no second deduction')

            saved = call('GET', path + '/records')
            assert saved['draft']['request'] == order
            assert saved['draft']['selections'] == [{'line_id': 'one', 'sku': 'B'}]
            assert saved['draft']['proposals'][0]['route'] == initial_route
            assert [p['state'] for p in saved['draft']['proposals']] == ['superseded', 'superseded', 'confirmed']
            assert len(saved['draft']['approved_substitutions']) == 1
            assert [p['version'] for p in saved['draft']['proposals']] == [1, 2, 3]
            call('POST', path + '/select', {'selections': [], 'expected_revision': draft['revision']}, status=409)
            call('POST', final + '/confirm', {}, headers={'X-Session-ID': sessions.session()['id']}, status=409)
            assert stock()['quantity'] == 0
            milestones.append('History retains all versions; confirmed orders cannot be edited or accessed by another session')
    finally:
        record_property('scenario', 'One deterministic HTTP workflow; not an LLM benchmark or customer result')
        record_property('business_time', NOW.isoformat())
        record_property('milestones_json', json.dumps(milestones, ensure_ascii=False))
        record_property('http_trace_json', json.dumps(trace, ensure_ascii=False))
        record_property('model_calls', 0)
        record_property('gpu_calls', 0)
