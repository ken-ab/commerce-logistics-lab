"""Transactional local commerce simulator. Never books shipping or charges money."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import uuid

from commerce_lab.catalog import Catalog
from logistics_lab.planning import audit_report, plan_fulfilment
from research.model_config import ROOT

WAREHOUSES = ('CN_SZ', 'HK', 'US_LAX')
DESTINATIONS = ('US', 'GB', 'ES', 'JP')
LANES = [
    ('SZ-HK', 'CN_SZ', 'HK', 'road', 1, 2, 1, 200),
    ('HK-LAX-AIR', 'HK', 'US_LAX', 'air', 3, 18, 6, 60),
    ('SZ-LAX-SEA', 'CN_SZ', 'US_LAX', 'sea', 22, 12, .4, 500),
    ('LAX-US', 'US_LAX', 'US', 'road', 3, 5, 1.2, 100),
    ('HK-GB-AIR', 'HK', 'GB', 'air', 5, 20, 7, 40),
    ('HK-ES-AIR', 'HK', 'ES', 'air', 6, 21, 8, 40),
    ('HK-JP-AIR', 'HK', 'JP', 'air', 2, 10, 3, 40),
    ('LAX-HK-AIR', 'US_LAX', 'HK', 'air', 4, 25, 6, 60),
]


def now():
    return datetime.now(timezone.utc).isoformat()


def initial_stock(product_id: str, warehouse: str) -> int:
    return int(hashlib.sha256(f'commerce-stock-v1:{product_id}:{warehouse}'.encode()).hexdigest()[:8], 16) % 31


class BusinessError(ValueError):
    pass


class Store:
    def __init__(self, catalog: Catalog | None = None, path: Path | None = None):
        self.catalog = catalog or Catalog()
        self.path = path or ROOT / 'data/business.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS seen(session_id TEXT,product_id TEXT,PRIMARY KEY(session_id,product_id));
                CREATE TABLE IF NOT EXISTS carts(session_id TEXT,product_id TEXT,quantity INTEGER CHECK(quantity>0),PRIMARY KEY(session_id,product_id));
                CREATE TABLE IF NOT EXISTS stock(product_id TEXT,warehouse TEXT,quantity INTEGER CHECK(quantity>=0),version INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(product_id,warehouse));
                CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,session_id TEXT,cart_hash TEXT,payload TEXT,created_at TEXT,order_id TEXT);
                CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,session_id TEXT,status TEXT,payload TEXT,created_at TEXT);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,session_id TEXT,kind TEXT,payload TEXT,created_at TEXT);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,session_id TEXT,status TEXT,request TEXT,result TEXT,created_at TEXT,updated_at TEXT);
                CREATE TABLE IF NOT EXISTS traces(id INTEGER PRIMARY KEY,run_id TEXT,agent TEXT,kind TEXT,payload TEXT,created_at TEXT);
                CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY,run_id TEXT,label TEXT,comment TEXT,created_at TEXT);
                CREATE TABLE IF NOT EXISTS skills(id TEXT PRIMARY KEY,version INTEGER,status TEXT,config TEXT,evidence TEXT,created_at TEXT);
                CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id,created_at);
                CREATE INDEX IF NOT EXISTS traces_run ON traces(run_id,id);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def session(self, session_id: str | None = None) -> dict:
        if session_id:
            with closing(self.connect()) as db:
                row = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
            if not row:
                raise BusinessError('Unknown local session')
            return dict(row)
        result = {'id': uuid.uuid4().hex, 'user_id': 'local-research-operator', 'created_at': now()}
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO sessions VALUES (:id,:user_id,:created_at)', result)
        return result

    def remember_products(self, session_id: str, products: list[dict]):
        self.session(session_id)
        with closing(self.connect()) as db, db:
            db.executemany('INSERT OR IGNORE INTO seen VALUES (?,?)', [(session_id, p['id']) for p in products])

    def stock(self, product_id: str, db=None) -> dict:
        if self.catalog.get(product_id) is None:
            raise BusinessError('Unknown product')
        if db is None:
            with closing(self.connect()) as connection:
                return self.stock(product_id, connection)
        rows = {r['warehouse']: dict(r) for r in db.execute('SELECT * FROM stock WHERE product_id=?', (product_id,))}
        return {'product_id': product_id, 'warehouses': [rows.get(wh, {'product_id': product_id,
            'warehouse': wh, 'quantity': initial_stock(product_id, wh), 'version': 0}) for wh in WAREHOUSES],
            'provenance': 'synthetic-research-v1'}

    def cart(self, session_id: str, db=None) -> dict:
        self.session(session_id)
        if db is None:
            with closing(self.connect()) as connection:
                return self.cart(session_id, connection)
        rows = db.execute('SELECT product_id,quantity FROM carts WHERE session_id=? ORDER BY product_id', (session_id,)).fetchall()
        items, total = [], 0
        for row in rows:
            product = self.catalog.get(row['product_id'])
            cents = round(product['price_usd'] * 100)
            total += cents * row['quantity']
            items.append({'product_id': product['id'], 'title': product['title'], 'price': product['price_usd'],
                          'quantity': row['quantity'], 'evidence_id': product['evidence_id']})
        return {'items': items, 'subtotal_usd': total / 100, 'currency': 'USD',
                'provenance': 'synthetic prices and local simulation cart'}

    def change_cart(self, session_id: str, product_id: str, quantity: int, *, add: bool = False) -> dict:
        if type(quantity) is not int or not 0 <= quantity <= 20:
            raise BusinessError('Quantity must be an integer from 0 through 20')
        self.session(session_id)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM seen WHERE session_id=? AND product_id=?', (session_id, product_id)).fetchone():
                raise BusinessError('Read this product from the catalog before changing its cart line')
            current = db.execute('SELECT quantity FROM carts WHERE session_id=? AND product_id=?', (session_id, product_id)).fetchone()
            target = quantity + (current[0] if current and add else 0)
            if target > 20:
                raise BusinessError('Cart quantity cap exceeded')
            if target > max(w['quantity'] for w in self.stock(product_id, db)['warehouses']):
                raise BusinessError('No warehouse has enough stock for this line')
            if target == 0:
                db.execute('DELETE FROM carts WHERE session_id=? AND product_id=?', (session_id, product_id))
            else:
                db.execute('INSERT INTO carts VALUES (?,?,?) ON CONFLICT(session_id,product_id) DO UPDATE SET quantity=excluded.quantity',
                           (session_id, product_id, target))
            self.event(db, session_id, 'cart_changed', {'product_id': product_id, 'quantity': target})
        return self.cart(session_id)

    @staticmethod
    def event(db, session_id: str, kind: str, payload: dict):
        db.execute('INSERT INTO events(session_id,kind,payload,created_at) VALUES (?,?,?,?)',
                   (session_id, kind, json.dumps(payload, ensure_ascii=False), now()))

    def world(self, product_ids: list[str], db=None) -> dict:
        products, warehouses = {}, {wh: {} for wh in WAREHOUSES}
        for ident in product_ids:
            product = self.catalog.get(ident)
            if not product:
                raise BusinessError('Unknown product')
            products[ident] = {'weight_kg': product['weight_kg'], 'minimum_units': 1, 'allowed_destinations': list(DESTINATIONS)}
            for row in self.stock(ident, db)['warehouses']:
                warehouses[row['warehouse']][ident] = row['quantity']
        legs = [dict(zip(('id', 'origin', 'destination', 'mode', 'days', 'fixed_usd', 'per_kg_usd', 'capacity_kg'), leg)) for leg in LANES]
        return {'dataset_id': 'commerce-logistics-simulation-v1', 'products': products, 'warehouses': warehouses, 'legs': legs}

    def quote(self, session_id: str, *, destination: str, deadline_days: float, shipping_budget_usd: float,
              blocked_legs: list[str] | None = None) -> dict:
        cart = self.cart(session_id)
        if not cart['items']:
            raise BusinessError('Cart is empty')
        if destination not in DESTINATIONS:
            raise BusinessError('Research environment destinations: US, GB, ES, JP')
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (deadline_days, shipping_budget_usd)):
            raise BusinessError('Deadline and shipping budget must be positive finite numbers')
        request = {'destination': destination, 'deadline_days': deadline_days, 'budget_usd': shipping_budget_usd,
                   'items': [{'sku': p['product_id'], 'quantity': p['quantity']} for p in cart['items']], 'blocked_legs': blocked_legs or []}
        world = self.world([p['product_id'] for p in cart['items']])
        plan = plan_fulfilment(request, world=world)
        audit = audit_report(request, plan, world=world)
        return {'request': request, 'plan': plan, 'audit': audit, 'cart': cart,
                'scope': 'Synthetic transport and stock; one warehouse per shipment; duties/taxes not estimated; no live carrier booking.'}

    def propose_order(self, session_id: str, **kwargs) -> dict:
        quote = self.quote(session_id, **kwargs)
        if not quote['audit']['passed']:
            return {**quote, 'proposal_id': None, 'status': quote['plan']['status']}
        ident = uuid.uuid4().hex
        cart_hash = hashlib.sha256(json.dumps(quote['cart'], sort_keys=True).encode()).hexdigest()
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO proposals VALUES (?,?,?,?,?,NULL)',
                       (ident, session_id, cart_hash, json.dumps(quote, ensure_ascii=False), now()))
        return {**quote, 'proposal_id': ident, 'status': 'awaiting_local_confirmation'}

    def confirm_proposal(self, session_id: str, proposal_id: str) -> dict:
        self.session(session_id)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            proposal = db.execute('SELECT * FROM proposals WHERE id=? AND session_id=?', (proposal_id, session_id)).fetchone()
            if not proposal:
                raise BusinessError('Unknown proposal for this session')
            if proposal['order_id']:
                return self.get_order(session_id, proposal['order_id'], db=db)
            cart = self.cart(session_id, db=db)
            if hashlib.sha256(json.dumps(cart, sort_keys=True).encode()).hexdigest() != proposal['cart_hash']:
                raise BusinessError('Cart changed; request a fresh quote')
            quote = json.loads(proposal['payload'])
            current_world = self.world([p['product_id'] for p in cart['items']], db=db)
            if not audit_report(quote['request'], quote['plan'], world=current_world)['passed']:
                raise BusinessError('Inventory or route invalidated this proposal; request a fresh quote')
            warehouse = quote['plan']['warehouse']
            for item in cart['items']:
                ident, quantity = item['product_id'], item['quantity']
                db.execute('INSERT OR IGNORE INTO stock VALUES (?,?,?,0)', (ident, warehouse, initial_stock(ident, warehouse)))
                changed = db.execute('UPDATE stock SET quantity=quantity-?,version=version+1 WHERE product_id=? AND warehouse=? AND quantity>=?',
                                     (quantity, ident, warehouse, quantity)).rowcount
                if changed != 1:
                    raise BusinessError('Concurrent order exhausted stock')
            order_id = 'SIM-' + uuid.uuid4().hex[:12].upper()
            total = round(cart['subtotal_usd'] * 100) + round(quote['plan']['total_cost_usd'] * 100)
            payload = {'order_id': order_id, 'status': 'processing', 'items': cart['items'], 'total': total / 100,
                       'currency': 'USD', 'shipping_plan': quote['plan'], 'placed_at': now(),
                       'provenance': 'local simulation order; no real payment, shipment or customer transaction'}
            db.execute('INSERT INTO orders VALUES (?,?,?,?,?)', (order_id, session_id, 'processing', json.dumps(payload, ensure_ascii=False), payload['placed_at']))
            db.execute('UPDATE proposals SET order_id=? WHERE id=?', (order_id, proposal_id))
            db.execute('DELETE FROM carts WHERE session_id=?', (session_id,))
            self.event(db, session_id, 'simulation_order_confirmed', {'proposal_id': proposal_id, 'order_id': order_id})
        return payload

    def get_order(self, session_id: str, order_id: str, db=None) -> dict | None:
        if db is None:
            with closing(self.connect()) as connection:
                return self.get_order(session_id, order_id, connection)
        row = db.execute('SELECT payload FROM orders WHERE id=? AND session_id=?', (order_id, session_id)).fetchone()
        return json.loads(row[0]) if row else None

    def orders(self, session_id: str, limit: int = 10) -> list[dict]:
        self.session(session_id)
        with closing(self.connect()) as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM orders WHERE session_id=? ORDER BY created_at DESC LIMIT ?',
                                                       (session_id, max(1, min(50, limit))))]

    def new_run(self, session_id: str, request: str, *, exclusive=False) -> str:
        self.session(session_id)
        ident = uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if exclusive and db.execute("SELECT 1 FROM runs WHERE session_id=? AND status IN ('queued','running')", (session_id,)).fetchone():
                raise BusinessError('A task is already running in this session')
            db.execute('INSERT INTO runs VALUES (?,?,?, ?,NULL,?,?)', (ident, session_id, 'queued', request, now(), now()))
        return ident

    def update_run(self, run_id: str, status: str, result: dict | None = None):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE runs SET status=?,result=?,updated_at=? WHERE id=?',
                       (status, json.dumps(result, ensure_ascii=False) if result else None, now(), run_id))

    def trace(self, run_id: str, agent: str, kind: str, payload: dict):
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO traces(run_id,agent,kind,payload,created_at) VALUES (?,?,?,?,?)',
                       (run_id, agent, kind, json.dumps(payload, ensure_ascii=False), now()))

    def run(self, session_id: str, run_id: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM runs WHERE id=? AND session_id=?', (run_id, session_id)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['result'] = json.loads(result['result']) if result['result'] else None
            result['traces'] = [{**dict(t), 'payload': json.loads(t['payload'])} for t in db.execute('SELECT * FROM traces WHERE run_id=? ORDER BY id', (run_id,))]
            return result
