from fastapi.testclient import TestClient

from commerce_lab.state import Store
from commerce_lab_v2.app import create_ui_app
from test_structured_report_v2 import Catalog, FIRST


def test_confirmation_status_survives_fresh_app_instance_and_remains_idempotent(tmp_path):
    store = Store(Catalog(), tmp_path/'store.sqlite')
    sid = store.session()['id']
    store.remember_products(sid, [store.catalog.get(FIRST)])
    store.change_cart(sid, FIRST, 1)
    proposal = store.propose_order(sid, destination='US', deadline_days=30, shipping_budget_usd=100)
    path = '/api/proposals/'+proposal['proposal_id']
    headers = {'X-Session-Id':sid}
    with TestClient(create_ui_app(store, testing=True)) as client:
        assert client.get(path,headers=headers).json()['status'] == 'awaiting_local_confirmation'
        first = client.post(path+'/confirm',headers=headers).json()
        assert client.get(path,headers=headers).json()['order_id'] == first['order_id']
    after_stock = store.stock(FIRST)
    with TestClient(create_ui_app(store, testing=True)) as client:
        restored = client.get(path,headers=headers).json()
        assert restored['status'] == 'confirmed' and restored['order_id'] == first['order_id']
        assert client.post(path+'/confirm',headers=headers).json() == first
    assert len(store.orders(sid)) == 1 and store.stock(FIRST) == after_stock


def test_proposal_status_is_scoped_to_session_and_does_not_create_orders(tmp_path):
    store = Store(Catalog(), tmp_path/'store.sqlite')
    owner, other = store.session()['id'],store.session()['id']
    store.remember_products(owner,[store.catalog.get(FIRST)])
    store.change_cart(owner,FIRST,1)
    proposal = store.propose_order(owner,destination='US',deadline_days=30,shipping_budget_usd=100)
    with TestClient(create_ui_app(store,testing=True)) as client:
        path='/api/proposals/'+proposal['proposal_id']
        assert client.get(path,headers={'X-Session-Id':other}).status_code == 404
        assert client.get(path,headers={'X-Session-Id':'unknown-session'}).status_code == 401
        assert client.get('/api/proposals/unknown',headers={'X-Session-Id':owner}).status_code == 404
        assert client.get(path,headers={'X-Session-Id':owner}).json()['order_id'] is None
    assert store.orders(owner) == []
