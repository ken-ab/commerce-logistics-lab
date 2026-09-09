from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from commerce_lab.state import BusinessError, Store, WAREHOUSES


class FakeCatalog:
    def get(self, ident):
        if ident != 'us:FIXTURE':
            return None
        return {'id': ident, 'title': 'Fixture shirt', 'price_usd': 12.34, 'weight_kg': .5,
                'evidence_id': 'esci:' + ident}


class BusinessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(FakeCatalog(), Path(temp.name) / 'business.sqlite')
        self.session = self.store.session()['id']
        self.product = 'us:FIXTURE'
        self.store.remember_products(self.session, [self.store.catalog.get(self.product)])
        with closing(self.store.connect()) as db, db:
            db.executemany('INSERT INTO stock VALUES (?,?,?,1)', [(self.product, w, 5) for w in WAREHOUSES])

    def proposal(self, session=None, quantity=2):
        session = session or self.session
        self.store.change_cart(session, self.product, quantity)
        return self.store.propose_order(session, destination='US', deadline_days=10, shipping_budget_usd=100)

    def test_order_is_atomic_and_confirmation_idempotent(self):
        proposal = self.proposal()
        order = self.store.confirm_proposal(self.session, proposal['proposal_id'])
        repeated = self.store.confirm_proposal(self.session, proposal['proposal_id'])
        self.assertEqual(order, repeated)
        stock = self.store.stock(self.product)['warehouses']
        self.assertEqual(sum(r['quantity'] for r in stock), 13)
        self.assertEqual(self.store.cart(self.session)['items'], [])

    def test_stale_cart_and_wrong_session_cannot_confirm(self):
        proposal = self.proposal()
        other = self.store.session()['id']
        with self.assertRaises(BusinessError):
            self.store.confirm_proposal(other, proposal['proposal_id'])
        self.store.change_cart(self.session, self.product, 3)
        with self.assertRaisesRegex(BusinessError, 'Cart changed'):
            self.store.confirm_proposal(self.session, proposal['proposal_id'])
        self.assertEqual(sum(r['quantity'] for r in self.store.stock(self.product)['warehouses']), 15)

    def test_two_competing_orders_cannot_oversell(self):
        other = self.store.session()['id']
        self.store.remember_products(other, [self.store.catalog.get(self.product)])
        first, second = self.proposal(quantity=4), self.proposal(other, quantity=4)
        def confirm(args):
            try:
                self.store.confirm_proposal(*args)
                return True
            except BusinessError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(confirm, [(self.session, first['proposal_id']), (other, second['proposal_id'])]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(sum(r['quantity'] for r in self.store.stock(self.product)['warehouses']), 11)

    def test_unseen_or_invalid_quantity_cannot_enter_cart(self):
        unseen = self.store.session()['id']
        for quantity in (True, -1, 21, 1.5):
            with self.assertRaises(BusinessError):
                self.store.change_cart(self.session, self.product, quantity)
        with self.assertRaises(BusinessError):
            self.store.change_cart(unseen, self.product, 1)

    def test_infeasible_quote_cannot_create_proposal(self):
        self.store.change_cart(self.session, self.product, 2)
        result = self.store.propose_order(self.session, destination='GB', deadline_days=1, shipping_budget_usd=1)
        self.assertEqual(result['status'], 'infeasible')
        self.assertIsNone(result['proposal_id'])

    def test_report_tampering_fails_at_transaction_boundary(self):
        proposal = self.proposal()
        with closing(self.store.connect()) as db, db:
            raw = json.loads(db.execute('SELECT payload FROM proposals WHERE id=?', (proposal['proposal_id'],)).fetchone()[0])
            raw['plan']['total_cost_usd'] = 0
            db.execute('UPDATE proposals SET payload=? WHERE id=?', (json.dumps(raw), proposal['proposal_id']))
        with self.assertRaises(BusinessError):
            self.store.confirm_proposal(self.session, proposal['proposal_id'])


if __name__ == '__main__':
    unittest.main()
