"""Build a separately versioned apparel world from preserved public ESCI rows."""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re

import duckdb

from apparel_fulfillment.data import digest, load_world, SIZES
from commerce_lab.catalog import DATASET, synthetic_fields

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/apparel_expansion_sources_v1'
DATA = ROOT / 'data/apparel_fulfillment_expansion_v1.json'
BRANDS = ('Champion', 'Fruit of the Loom', 'Gildan', 'Hanes')
PER_BRAND_LIMIT = 60
SOURCE = ROOT / 'upstream/esci-data/shopping_queries_dataset/shopping_queries_dataset_products.parquet'


def sha(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def write(path, value):
    with path.open('x', encoding='utf-8') as file:
        file.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def derive(raw):
    title, color = raw['product_title'], raw['product_color']
    size = re.search(r', (X-Small|Small|Medium|Large|X-Large|XX-Large)$', title or '')
    if not size or not color or not color.strip():
        return None, 'unresolved_size_suffix_or_color'
    lower = title.casefold()
    if any(word in lower for word in ('backpack', 'dress shirt', 'joggers', 'sweatpants', 'uniform set')):
        return None, 'ambiguous_or_non_target_garment'
    categories = [name for name, pattern in [('t_shirt', r'\bt-shirts?\b'), ('hoodie', r'\bhoodies?\b'),
                                             ('polo', r'\bpolo\b')] if re.search(pattern, lower)]
    if len(categories) != 1:
        return None, 'ambiguous_or_missing_category'
    sizes = {int(a or b) for a, b in re.findall(r'\b(\d+)[ -]*pack\b|\bpack of (\d+)\b', lower)}
    if len(sizes) > 1 or any(n < 1 or n > 20 for n in sizes):
        return None, 'ambiguous_or_out_of_scope_pack_count'
    pack = next(iter(sizes), 1)
    audience = next((name for name, pattern in [('women', r'\bwom[ae]n\b|\bladies\b'), ('girls', r'\bgirls?\b'),
        ('boys', r'\bboys?\b'), ('men', r'\bmen\b'), ('children', r'\bkids?\b|\byouth\b'),
        ('unisex', r'\bunisex\b')] if re.search(pattern, lower)), None)
    sku = 'us:' + raw['product_id']
    prefix = title.rsplit(',', 2)[0]
    style = 'EXP-' + digest({'brand': raw['product_brand'], 'prefix': prefix})[:16]
    text = '\n'.join(raw.get(k) or '' for k in ('product_bullet_point', 'product_description'))
    # Preserve full cleaned text, with the exact raw fields retained separately.
    description = html.unescape(re.sub(r'<[^>]*>', ' ', text)).strip()
    public = {'id': sku, 'title': title, 'brand': raw['product_brand'], 'color': color, 'description': description}
    source = 'esci:' + sku
    price, _ = synthetic_fields(sku)
    piece_weight = {'t_shirt': 180, 'hoodie': 500, 'polo': 260}[categories[0]]
    provenance = {
        'sku': {'kind': 'public_asin_used_as_lab_sku', 'evidence_id': source + ':id'},
        'brand': {'kind': 'public_metadata', 'evidence_id': source + ':brand'},
        'color': {'kind': 'normalized_public_metadata', 'evidence_id': source + ':color'},
        'size': {'kind': 'normalized_title', 'evidence_id': source + ':title', 'raw': size.group(1)},
        'category': {'kind': 'title_classification', 'evidence_id': source + ':title'},
        'style_id': {'kind': 'research_grouping', 'evidence_id': 'lab-style:' + style},
        'pieces_per_catalog_unit': {'kind': 'explicit_title_pack_count' if sizes else 'merchant_simulation',
            'evidence_id': source + ':title' if sizes else 'sim-unit:' + sku},
        'weight_grams_per_catalog_unit': {'kind': 'merchant_simulation', 'evidence_id': 'sim-weight:' + sku},
        'unit_price_cents': {'kind': 'merchant_simulation', 'evidence_id': 'sim-price:' + sku},
    }
    if audience:
        provenance['audience'] = {'kind': 'title_classification', 'evidence_id': source + ':title'}
    return {'sku': sku, 'style_id': style, 'category': categories[0], 'audience': audience,
        'brand': raw['product_brand'], 'color': color.strip().casefold(), 'size': SIZES[size.group(1)], 'title': title,
        'pieces_per_catalog_unit': pack, 'weight_grams_per_catalog_unit': piece_weight * pack,
        'unit_price_cents': price, 'version': 1, 'source_record': public, 'source_record_sha256': digest(public),
        'raw_source_record_sha256': digest(raw), 'provenance': provenance}, None


def build():
    if OUT.exists() or DATA.exists():
        raise FileExistsError('Preserve existing expanded snapshots')
    audit = json.loads((ROOT / 'evidence/esci_data_audit.json').read_text(encoding='utf-8'))
    expected = next(f['actual_sha256'] for f in audit['files'] if f['file'] == SOURCE.name)
    assert sha(SOURCE) == expected
    db = duckdb.connect()
    db.execute('SET threads=4')
    columns = [r[0] for r in db.execute('DESCRIBE SELECT * FROM read_parquet(?)', [str(SOURCE)]).fetchall()]
    values = db.execute('''SELECT * FROM read_parquet(?) WHERE product_locale='us'
        AND product_brand IN ('Champion','Fruit of the Loom','Gildan','Hanes')
        AND (product_title ILIKE '%t-shirt%' OR product_title ILIKE '%polo%' OR product_title ILIKE '%hoodie%')
        ORDER BY product_brand,product_id''', [str(SOURCE)]).fetchall()
    db.close()
    world = deepcopy(load_world())
    old = set(world['variants'])
    selected, skipped, accepted_by_brand = {}, Counter(), Counter()
    excluded = []
    for row in values:
        raw = dict(zip(columns, row))
        variant, reason = derive(raw)
        if reason:
            skipped[reason] += 1
            excluded.append({'product_id': raw['product_id'], 'brand': raw['product_brand'], 'reason': reason})
            continue
        brand = variant['brand']
        if accepted_by_brand[brand] >= PER_BRAND_LIMIT:
            skipped['deterministic_per_brand_cap'] += 1
            continue
        sku = variant['sku']
        assert sku not in world['variants']
        accepted_by_brand[brand] += 1
        selected[sku] = raw
        world['variants'][sku] = variant
        world['stock'][sku] = {'available_catalog_units': 120, 'version': 1, 'evidence_id': 'sim-stock:CN-SZ:' + sku}
        world['styles'][variant['style_id']] = {'brand': brand, 'title_prefix': raw['product_title'].rsplit(',', 2)[0],
            'relationship': 'research grouping, not manufacturer-confirmed parent ASIN'}
    assert all(accepted_by_brand[brand] == PER_BRAND_LIMIT for brand in BRANDS), accepted_by_brand
    for brand in BRANDS:
        world['brand_rules'][brand] = {'allowed_sales_regions': ['DE', 'GB', 'US'],
            'wholesale_minimum_pieces_per_sku': 10, 'version': 1, 'evidence_id': 'sim-brand-policy:' + brand}
    world['dataset_id'] = 'apparel-expansion-v1-20260909'
    world['selection'] = {'rule': 'Existing 37 variants retained byte-for-byte as JSON values; add first 60 valid ASIN-sorted rows per named new brand after explicit size/color/category/pack checks.',
        'base_variants': len(old), 'added_variants': len(selected), 'skipped': dict(skipped), 'rows': len(world['variants'])}
    world['notice'] = ('Historical public ESCI product text, plus explicitly simulated inventory, price, weight, units where not explicit, '
        'brand rules and style relationships. Added variants retain full cleaned bullet/description fields and separately archived raw records. '
        'No real stock, manufacturer-approved variant relationship or distribution rights claimed.')
    OUT.mkdir()
    write(OUT / 'raw_records.json', selected)
    write(OUT / 'excluded_records.json', excluded)
    write(DATA, world)
    manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'source_commit': audit['commit'],
        'source_url': audit['source'], 'source_dataset': DATASET, 'source_file_sha256': expected,
        'license': audit['license'], 'selection_rule': world['selection']['rule'], 'rows_considered': len(values),
        'skipped': dict(skipped), 'base_variants': len(old), 'added_variants': len(selected),
        'added_per_brand': dict(accepted_by_brand), 'total_variants': len(world['variants']),
        'added_categories': dict(Counter(world['variants'][sku]['category'] for sku in selected)),
        'added_audiences': dict(Counter(str(world['variants'][sku]['audience']) for sku in selected)),
        'added_pack_counts': dict(Counter(world['variants'][sku]['pieces_per_catalog_unit'] for sku in selected)),
        'full_cleaned_descriptions_over_1200': sum(len(world['variants'][sku]['source_record']['description']) > 1200 for sku in selected),
        'maximum_cleaned_description_length': max(len(world['variants'][sku]['source_record']['description']) for sku in selected),
        'dataset_sha256': sha(DATA), 'raw_records_sha256': sha(OUT / 'raw_records.json'),
        'builder_sha256': sha(Path(__file__)), 'source_fields_not_human_verified_for_every_product': True,
        'new_users': 0, 'new_queries_or_agent_runs': 0, 'new_model_calls': 0, 'default_workspace_world_changed': False,
        'scope': 'New to apparel research snapshot, not unseen by model pretraining or the earlier complete ESCI ingestion.'}
    write(OUT / 'manifest.json', manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    build()
