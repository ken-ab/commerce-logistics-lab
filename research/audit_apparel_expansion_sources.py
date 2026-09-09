"""Reconcile all added records against raw Parquet, without using the builder."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/apparel_expansion_sources_v1'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def main():
    target = OUT / 'audit.json'
    if target.exists():
        raise FileExistsError('Preserve completed data audit')
    manifest, raw = read(OUT / 'manifest.json'), read(OUT / 'raw_records.json')
    world, old = read(ROOT / 'data/apparel_fulfillment_expansion_v1.json'), read(ROOT / 'data/apparel_fulfillment_v1.json')
    assert len(world['variants']) == 277 and len(raw) == 240 and not set(raw).intersection(old['variants'])
    for sku, variant in old['variants'].items():
        assert world['variants'][sku] == variant and world['stock'][sku] == old['stock'][sku]
    source = ROOT / 'upstream/esci-data/shopping_queries_dataset/shopping_queries_dataset_products.parquet'
    with source.open('rb') as file:
        assert hashlib.file_digest(file, 'sha256').hexdigest() == manifest['source_file_sha256']
    db = duckdb.connect()
    db.execute('SET threads=4')
    columns = [r[0] for r in db.execute('DESCRIBE SELECT * FROM read_parquet(?)', [str(source)]).fetchall()]
    rows = db.execute("SELECT * FROM read_parquet(?) WHERE product_locale='us' AND product_id IN (" + ','.join('?' for _ in raw) + ')',
                      [str(source), *[sku.split(':')[1] for sku in raw]]).fetchall()
    db.close()
    observed = {'us:' + row[0]: dict(zip(columns, row)) for row in rows}
    assert observed == raw
    mapping = {'X-Small': 'XS', 'Small': 'S', 'Medium': 'M', 'Large': 'L', 'X-Large': 'XL', 'XX-Large': 'XXL'}
    checked, ambiguous = [], []
    for sku, row in sorted(raw.items()):
        v = world['variants'][sku]
        assert v['raw_source_record_sha256'] == digest(row)
        assert v['source_record_sha256'] == digest(v['source_record'])
        assert v['source_record']['title'] == row['product_title'] and v['title'] == row['product_title']
        assert v['brand'] == v['source_record']['brand'] == row['product_brand']
        assert v['source_record']['color'] == row['product_color'] and v['color'] == row['product_color'].strip().casefold()
        assert v['size'] == mapping[row['product_title'].rsplit(', ', 1)[-1]]
        source_text = '\n'.join(row.get(k) or '' for k in ('product_bullet_point', 'product_description'))
        assert v['source_record']['description'] == html.unescape(re.sub('<[^>]*>', ' ', source_text)).strip()
        basis = v['provenance']['pieces_per_catalog_unit']['kind']
        if basis == 'explicit_title_pack_count':
            count = str(v['pieces_per_catalog_unit'])
            assert re.search(r'\b' + count + r'[ -]*pack\b|\bpack of ' + count + r'\b', row['product_title'], re.I)
        else:
            assert basis == 'merchant_simulation' and v['pieces_per_catalog_unit'] == 1
        if v['audience'] is None:
            ambiguous.append(sku)
        for field in ('weight_grams_per_catalog_unit', 'unit_price_cents'):
            assert v['provenance'][field]['kind'] == 'merchant_simulation'
        checked.append({'sku': sku, 'raw_matches_parquet': True, 'normalized_record_matches': True,
                        'pack_unit_basis': basis, 'audience_unresolved': v['audience'] is None})
    result = {'checked_at': datetime.now(timezone.utc).isoformat(), 'passed': True, 'raw_rows_reconciled': len(rows),
        'base_variants_preserved': 37, 'total_variants': 277, 'audience_unresolved_skus': ambiguous,
        'normalization_checks': checked, 'new_model_calls': 0, 'new_real_users': 0,
        'scope': 'Exact raw-file equality and deterministic normalization checks; not human semantic verification of every product or current manufacturer stock.',
        'hashes': {str(p.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                   (OUT / 'manifest.json', OUT / 'raw_records.json', OUT / 'excluded_records.json',
                    ROOT / 'data/apparel_fulfillment_expansion_v1.json', ROOT / 'research/apparel_expansion_data.py')}}
    with target.open('x', encoding='utf-8') as file:
        file.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('normalization_checks', 'hashes')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
