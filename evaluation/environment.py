"""Fresh business environments and canonical state, shared by live and replay runs."""
from contextlib import closing
import json
from pathlib import Path

from commerce_lab.catalog import Catalog
from commerce_lab.state import Store, WAREHOUSES


def canonical(value):
    """Ignore generated identity/time only; keep all business facts and product IDs."""
    if isinstance(value, dict):
        redundant = {'status'} if {'request', 'plan', 'audit'} <= value.keys() else set()
        return {k: canonical(v) for k, v in value.items()
                if k not in {'proposal_id', 'order_id', 'placed_at', 'created_at'} | redundant}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    return value


class CaseCatalog:
    def __init__(self, case, base=None):
        self.base = base or Catalog()
        self.case = case

    def overlay(self, row):
        if row and row['id'] == self.case.get('product_id') and self.case.get('attack_text'):
            row = dict(row)
            row['description'] += '\n' + self.case['attack_text']
        return row

    def get(self, ident):
        return self.overlay(self.base.get(ident))

    def search(self, *args, **kwargs):
        return [self.overlay(row) for row in self.base.search(*args, **kwargs)]


def create_case_store(case, path: Path, *, catalog=None):
    if path.exists():
        raise ValueError('Evaluation database already exists; never reuse mutable state across trials')
    store = Store(CaseCatalog(case, catalog), path)
    session = store.session()['id']
    ident = case.get('product_id')
    if ident:
        if not store.catalog.get(ident):
            raise ValueError('Case product is not in the actual public catalog')
        with closing(store.connect()) as db, db:
            db.executemany('INSERT INTO stock VALUES (?,?,?,0)',
                [(ident, wh, case.get('stock', {}).get(wh, 10)) for wh in WAREHOUSES])
    for item in case.get('initial_cart', []):
        store.remember_products(session, [store.catalog.get(item['product_id'])])
        store.change_cart(session, **item)
    return store, session


def business_snapshot(store, session, last_quote=None):
    with closing(store.connect()) as db:
        stock = [dict(r) for r in db.execute('SELECT * FROM stock ORDER BY product_id,warehouse')]
        proposals = db.execute('SELECT payload FROM proposals WHERE session_id=? ORDER BY created_at DESC LIMIT 1', (session,)).fetchall()
    # Exclude read-cache, events, run logs, UUIDs and timestamps. Retain actual
    # inventory, cart, latest proposal and observed quote. Repeated valid planning
    # paths can therefore reach the same end state without identical tool sequences.
    return canonical({'cart': store.cart(session), 'stock': stock,
        'latest_proposal': json.loads(proposals[0][0]) if proposals else None,
        'last_quote': last_quote, 'orders': store.orders(session)})
