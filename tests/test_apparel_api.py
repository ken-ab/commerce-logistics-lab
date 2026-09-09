"""HTTP workflow checks with isolated state; no paid model or GPU calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from apparel_fulfillment.api import install_apparel
from apparel_fulfillment.store import ApparelStore
from commerce_lab.state import Store
from test_apparel_orders import fixture, request


@pytest.fixture
def workspace(tmp_path):
    world = fixture()
    world['notice'] = 'Explicit isolated simulation'
    for v in world['variants'].values():
        v['provenance']['weight_grams_per_catalog_unit'] = {'evidence_id': 'fixture-weight'}
    sessions = Store(catalog=object(), path=tmp_path / 'sessions.sqlite')
    store = ApparelStore(tmp_path / 'apparel.sqlite', world=world)
    app = install_apparel(FastAPI(), operations=store, sessions=sessions)
    with TestClient(app) as client:
        client.headers['X-Session-ID'] = sessions.session()['id']
        yield client, store, sessions


def selected(client, *, shipping=False):
    req = request()
    req['needs_shipping'] = shipping
    if shipping:
        now = datetime.now(timezone.utc)
        req['shipping'] = {'destination': 'DE-DC', 'ready_at': (now + timedelta(days=1)).isoformat(),
                           'deadline_at': (now + timedelta(days=7)).isoformat(), 'budget_cents': 20000}
    draft = client.post('/api/apparel/drafts', json={'order': req}).json()
    draft = client.post(f'/api/apparel/drafts/{draft["id"]}/select', json={
        'selections': [{'line_id': 'one', 'sku': 'A'}], 'expected_revision': draft['revision']}).json()
    assert draft['order_check']['status'] == 'ready'
    return draft, f'/api/apparel/drafts/{draft["id"]}'


def test_session_owner_revision_and_extra_fields(workspace):
    client, store, sessions = workspace
    assert client.get('/api/apparel/catalog').json()['dataset_id'] == 'isolated-test-fixture'
    draft, path = selected(client)
    assert client.post(path + '/select', json={'selections': [], 'expected_revision': 1}).status_code == 409
    assert client.post(path + '/proposals', json={'expected_revision': True}).status_code == 422
    assert client.post(path + '/proposals', json={'expected_revision': draft['revision'], 'approved': True}).status_code == 422
    assert client.get(path, headers={'X-Session-ID': sessions.session()['id']}).status_code == 409
    assert client.get(path, headers={'X-Session-ID': 'missing-session'}).status_code == 401


def test_cancel_revise_confirm_and_export_preserve_versions(workspace):
    client, store, sessions = workspace
    draft, path = selected(client, shipping=True)
    p1 = client.post(path + '/proposals', json={'expected_revision': draft['revision']}).json()
    original = deepcopy(p1['route'])
    assert client.get(path + f'/proposals/{p1["proposal_id"]}/validity').json()['valid']
    flight = next(s for s in original['segments'] if s['mode'] == 'air')
    event = {'event_id': 'API-cancel', 'kind': 'cancel', 'leg_id': flight['leg_id'],
             'nominal_departure': flight['nominal_departure'], 'published_at': datetime.now(timezone.utc).isoformat()}
    assert client.post('/api/apparel/events', json=event).status_code == 200
    assert not client.get(path + f'/proposals/{p1["proposal_id"]}/validity').json()['valid']
    assert client.post(path + f'/proposals/{p1["proposal_id"]}/confirm', json={}).json()['status'] == 'rejected'
    p2 = client.post(path + '/proposals', json={'expected_revision': draft['revision']}).json()
    assert p2['version'] == 2 and p2['previous_proposal_id'] == p1['proposal_id']
    confirmation = client.post(path + f'/proposals/{p2["proposal_id"]}/confirm', json={}).json()
    assert confirmation['status'] == 'confirmed_simulation'
    assert client.post(path + f'/proposals/{p2["proposal_id"]}/confirm', json={}).json()['idempotent_replay']
    records = client.get(path + '/records').json()
    assert records['draft']['proposals'][0]['route'] == original
    assert records['draft']['proposals'][0]['state'] == 'superseded'
    assert records['draft']['proposals'][1]['state'] == 'confirmed'
    assert any(t['kind'] == 'confirmation_rejected' for t in records['traces'])


def test_substitution_needs_explicit_approval_and_never_bypasses_rules(workspace):
    client, store, sessions = workspace
    draft, path = selected(client)
    changed = client.post(path + '/select', json={'selections': [{'line_id': 'one', 'sku': 'B'}],
                           'expected_revision': draft['revision']}).json()
    assert changed['order_check']['status'] == 'needs_clarification'
    assert client.post(path + '/proposals', json={'expected_revision': changed['revision']}).json()['status'] == 'order_not_ready'
    approval = changed['order_check']['substitution_proposals'][0]['approval_id']
    changed = client.post(path + '/approve-substitution', json={'approval_id': approval,
                            'expected_revision': changed['revision']}).json()
    assert changed['order_check']['status'] == 'ready'
    p = client.post(path + '/proposals', json={'expected_revision': changed['revision']}).json()
    assert p['route']['status'] == 'not_required'
    store.set_simulated_stock('B', 0, expected_version=1)
    assert client.post(path + f'/proposals/{p["proposal_id"]}/confirm', json={}).json()['status'] == 'rejected'
