"""Immutable ESCI catalog with FTS5 retrieval; synthetic commercial fields are labeled."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import argparse
from pathlib import Path
import re
import sqlite3
import time

from research.model_config import ROOT

CATALOG = ROOT / 'data/catalog.sqlite'
DATASET = 'amazon-esci-7916cdf6'


def synthetic_fields(product_id: str) -> tuple[int, int]:
    number = int.from_bytes(hashlib.sha256(('commerce-research-v1:' + product_id).encode()).digest()[:8], 'big')
    return 500 + number % 19501, 100 + (number // 20000) % 1901


def finalize_catalog(db, audit, count, started, *, resumed=False):
    print(json.dumps({'phase': 'fts_index', 'products': count}), flush=True)
    with db:
        if count != audit['products']['rows']:
            raise RuntimeError('Ingestion count differs from source audit')
        locales = dict(db.execute('SELECT locale,COUNT(*) FROM products GROUP BY locale'))
        if locales != {r['product_locale']: r['rows'] for r in audit['product_locales']}:
            raise RuntimeError('Locale counts differ from source audit')
        db.execute("INSERT INTO products_fts(products_fts) VALUES('rebuild')")
        db.execute("INSERT INTO products_fts(products_fts) VALUES('integrity-check')")
        manifest = {'built_at': datetime.now(timezone.utc).isoformat(), 'source_commit': audit['commit'],
            'products': count, 'locales': locales, 'elapsed_seconds': round(time.monotonic() - started, 2),
            'elapsed_scope': 'index completion after resuming imported rows' if resumed else 'import and indexing',
            'resumed_after_manifest_field_fix': resumed,
            'catalog_source': 'Amazon ESCI public product metadata, Apache-2.0',
            'description_storage': 'HTML tags stripped; first 1200 characters of bullets plus description. Raw Parquet preserved.',
            'synthetic_fields': ['price_cents', 'weight_grams'], 'real_application_users': 0,
            'index': 'SQLite FTS5 BM25 over title,brand,color,description; English is the initial evaluation locale.'}
        db.execute('INSERT INTO metadata VALUES (?,?)', ('manifest', json.dumps(manifest)))
        db.execute("INSERT INTO metadata VALUES ('complete','true')")
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    (ROOT / 'evidence/catalog_ingestion.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return manifest


def build_catalog(path: Path = CATALOG, source: Path | None = None, *, finish_imported=False) -> dict:
    import duckdb
    source = source or ROOT / 'upstream/esci-data/shopping_queries_dataset/shopping_queries_dataset_products.parquet'
    audit = json.loads((ROOT / 'evidence/esci_data_audit.json').read_text(encoding='utf-8'))
    if not audit.get('download_complete_and_verified'):
        raise RuntimeError('Audit both public source files before ingestion')
    if path.exists():
        with closing(sqlite3.connect(path)) as db:
            if db.execute("SELECT value FROM metadata WHERE key='complete'").fetchone() == ('true',):
                return json.loads(db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()[0])
            if finish_imported:
                count = db.execute('SELECT COUNT(*) FROM products').fetchone()[0]
                return finalize_catalog(db, audit, count, time.monotonic(), resumed=True)
        raise RuntimeError('Incomplete catalog exists; inspect it before rebuilding')
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    count = 0
    with closing(sqlite3.connect(path)) as db, closing(duckdb.connect()) as reader:
        db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE products(rowid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
                product_id TEXT NOT NULL, locale TEXT NOT NULL, title TEXT NOT NULL,
                brand TEXT NOT NULL, color TEXT NOT NULL, description TEXT NOT NULL,
                price_cents INTEGER NOT NULL, weight_grams INTEGER NOT NULL);
            CREATE INDEX products_locale ON products(locale);
            CREATE VIRTUAL TABLE products_fts USING fts5(title,brand,color,description,
                content='products',content_rowid='rowid',tokenize='unicode61 remove_diacritics 2');
        ''')
        reader.execute("SET threads=4")
        reader.execute("SET memory_limit='4GB'")
        reader.execute('''SELECT product_locale,product_id,product_title,product_brand,
            product_color,coalesce(product_bullet_point,'') || '\n' || coalesce(product_description,'')
            FROM read_parquet(?) ORDER BY product_locale,product_id''', [str(source)])
        while rows := reader.fetchmany(5000):
            prepared = []
            for locale, product_id, title, brand, color, description in rows:
                ident = f'{locale}:{product_id}'
                price, weight = synthetic_fields(ident)
                clean = re.sub(r'<[^>]{0,300}>', ' ', description)
                prepared.append((ident, product_id, locale, title, brand or '', color or '', clean[:1200], price, weight))
            with db:
                db.executemany('''INSERT INTO products(id,product_id,locale,title,brand,color,description,price_cents,weight_grams)
                                  VALUES (?,?,?,?,?,?,?,?,?)''', prepared)
            count += len(prepared)
            if count % 100000 == 0:
                print(json.dumps({'phase': 'catalog_import', 'products': count}), flush=True)
        manifest = finalize_catalog(db, audit, count, started)
    return manifest


class Catalog:
    def __init__(self, path: Path = CATALOG):
        self.path = path

    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def present(row) -> dict:
        data = dict(row)
        data.pop('rowid', None)
        data['price_usd'] = data.pop('price_cents') / 100
        data['weight_kg'] = data.pop('weight_grams') / 1000
        data['evidence_id'] = 'esci:' + data['id']
        data['provenance'] = {'product_metadata': DATASET, 'price_and_weight': 'synthetic-research-v1',
                              'inventory_and_transport': 'synthetic-research-v1'}
        return data

    def get(self, product_id: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
        return self.present(row) if row else None

    def search(self, query: str, *, locale: str = 'us', limit: int = 8,
               max_price: float | None = None, min_price: float | None = None,
               color: str | None = None, match_mode: str = 'all') -> list[dict]:
        if locale not in {'us', 'es', 'jp'} or match_mode not in {'all', 'any'}:
            raise ValueError('Unknown catalog locale or search policy')
        tokens = re.findall(r'[^\W_]+', query.lower(), re.UNICODE)[:14]
        tokens = [t for t in tokens if t not in {'a', 'an', 'the', 'for', 'of', 'with', 'please', 'find', 'me'}]
        if not tokens:
            return []
        expression = (' AND ' if match_mode == 'all' else ' OR ').join('"' + t + '"' for t in tokens)
        conditions, params = ['products_fts MATCH ?', 'p.locale=?'], [expression, locale]
        for value, clause in ((max_price, 'p.price_cents<=?'), (min_price, 'p.price_cents>=?')):
            if value is not None:
                conditions.append(clause)
                params.append(round(float(value) * 100))
        if color:
            conditions.append('LOWER(p.color) LIKE ?')
            params.append('%' + color.lower() + '%')
        params.append(max(1, min(limit, 100)))
        with closing(self.connect()) as db:
            rows = db.execute('SELECT p.*,bm25(products_fts,5,2,2,0.5) AS retrieval_score FROM products_fts '
                'JOIN products p ON p.rowid=products_fts.rowid WHERE ' + ' AND '.join(conditions)
                + ' ORDER BY retrieval_score,p.id LIMIT ?', params).fetchall()
        return [self.present(row) for row in rows]


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--finish-imported', action='store_true')
    args = parser.parse_args()
    print(json.dumps(build_catalog(finish_imported=args.finish_imported), ensure_ascii=False, indent=2))
