"""Versioned, source-scoped candidate retrieval; no selection or approval writes.

The merchant's verified variants define the searchable universe before top-k.
Explicit structured filters are exact, independent of lexical matching or scores.
"""
from contextlib import closing
from copy import deepcopy
import re
import sqlite3

from commerce_lab.retrieval import RerankedCatalog, request_scores

VERSION = 'apparel-source-scoped-search-v1'
FILTERS = ('brand', 'size', 'color', 'style_id')
STOP = {'a', 'an', 'the', 'for', 'of', 'with', 'please', 'find', 'me'}


class FixedCandidates:
    def __init__(self, rows):
        self.rows = rows

    def search(self, query, *, limit=100):
        return deepcopy(self.rows[:limit])


class ScopedCatalog:
    """A small merchant FTS view, built from existing source records only."""

    def __init__(self, world, filters):
        self.world = world
        self.filters = filters
        self.matches = 0
        self.rows = {}
        for sku, variant in world['variants'].items():
            source = variant['source_record']
            if variant['sku'] != sku or source['id'] != sku:
                raise ValueError('Variant identity does not match its source')
            self.rows[sku] = {'id': sku, **{key: source.get(key, '') for key in
                ('title', 'brand', 'color', 'description')}}
        self.eligible = sorted(sku for sku, variant in world['variants'].items() if all(
            value is None or str(variant.get(key, '')).casefold() == value.casefold()
            for key, value in filters.items()))

    def search(self, query, *, limit=100, match_mode='any'):
        self.matches = 0
        if not self.eligible:
            return []
        # Direct identity lookup still respects all structured filters.
        if query.strip() in self.rows:
            ids = [query.strip()] if query.strip() in self.eligible else []
        else:
            tokens = [t for t in re.findall(r'[^\W_]+', query.lower(), re.UNICODE)[:14] if t not in STOP]
            if not tokens:
                return []
            expression = (' AND ' if match_mode == 'all' else ' OR ').join('"' + t + '"' for t in tokens)
            with closing(sqlite3.connect(':memory:')) as db:
                db.execute("CREATE VIRTUAL TABLE variants_fts USING fts5(sku UNINDEXED,title,brand,color,description,tokenize='unicode61 remove_diacritics 2')")
                db.executemany('INSERT INTO variants_fts VALUES (?,?,?,?,?)',
                    [(sku, row['title'], row['brand'], row['color'], row['description']) for sku, row in self.rows.items()])
                found = db.execute('SELECT sku FROM variants_fts WHERE variants_fts MATCH ? AND sku IN ('
                    + ','.join('?' for _ in self.eligible)
                    + ') ORDER BY bm25(variants_fts,0,5,2,2,0.5),sku', [expression, *self.eligible]).fetchall()
                ids = [row[0] for row in found]
        self.matches = len(ids)
        return [deepcopy(self.rows[sku]) for sku in ids[:limit]]


def search_variants(world, arguments, *, match_mode='any', scorer=request_scores, limit=12):
    if match_mode not in ('all', 'any'):
        raise ValueError('Unknown lexical matching policy')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Result limit must be an integer from 1 to 100')
    if not isinstance(arguments, dict) or set(arguments) - {*FILTERS, 'query'}:
        raise ValueError('Unknown apparel search fields')
    for key, value in arguments.items():
        if value is not None and (not isinstance(value, str) or len(value) > 1000):
            raise ValueError('Search fields must be bounded text or null')
    filters = {key: arguments.get(key) for key in FILTERS}
    catalog = ScopedCatalog(world, filters)
    query = arguments.get('query') or ''
    if not query.strip():
        ids = catalog.eligible[:limit]
        matches = len(catalog.eligible)
        ranking = {'method': 'source_scoped_browse', 'candidate_count': matches}
    else:
        candidates = catalog.search(query, limit=100, match_mode=match_mode)
        if len(candidates) > 1:
            rows = RerankedCatalog(FixedCandidates(candidates), scorer=scorer).search(query, limit=limit)
            ranking = rows[0]['retrieval']
        else:
            rows = candidates
            ranking = {'method': 'single_filtered_candidate' if rows else 'empty_lexical_candidates',
                       'candidate_count': len(rows)}
        ids, matches = [r['id'] for r in rows], catalog.matches
    variants = []
    for sku in ids:
        variant = world['variants'][sku]
        variants.append({key: deepcopy(variant[key]) for key in
            ('sku', 'title', 'style_id', 'brand', 'color', 'size', 'pieces_per_catalog_unit')} | {
            'stock_catalog_units': world['stock'][sku]['available_catalog_units'],
            'source_record_sha256': variant['source_record_sha256'],
            'field_provenance': deepcopy(variant['provenance'])})
    return {'matches': matches, 'variants': variants, 'truncated': matches > len(ids), 'dataset_id': world['dataset_id'],
        'retrieval': {**ranking, 'version': VERSION, 'match_mode': match_mode, 'filters': filters,
            'merchant_source_variants': len(catalog.rows), 'eligible_variants_before_ranking': len(catalog.eligible),
            'scope': 'Merchant source-linked variants; exact filters before top-k. Ranking is not order verification.',
            'order_mutated': False, 'substitution_approved': False}}
