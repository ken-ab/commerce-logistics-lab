"""Current product-material receipts for order operations; no research labels."""
from copy import deepcopy

from apparel_fulfillment.data import digest
from apparel_fulfillment.orders import OrderError

VERSION = 'apparel-current-product-sources-v1'
GUIDE = ('For product inspection, staging a candidate, preparing or reviewing a proposal, read the '
         'current selected product material using read_variant before finishing. The required SKUs follow '
         'the actual selection, including specifically approved replacements. A partial order snapshot or '
         'search result is not the complete source record. A successful full read in this run can be reused '
         'while its product, provenance, stock and brand-rule snapshot still matches. Read missing or stale '
         'SKUs listed here. This check never selects, approves, confirms, or reads material on your behalf.')


def required_skus(contract, current):
    if contract.mode == 'inspect_product':
        return [contract.product_sku]
    if contract.mode in ('stage_candidate', 'prepare_proposal', 'review_proposal'):
        return sorted({p['sku'] for p in current['selections']})
    return []


def full_material(world, sku):
    if sku not in world['variants']:
        raise OrderError('Unknown source-linked variant')
    variant = deepcopy(world['variants'][sku])
    if variant.get('source_record_sha256') != digest(variant.get('source_record')):
        raise OrderError('The product source record does not match its recorded hash; correct the source data before use')
    return {'variant': variant, 'stock': deepcopy(world['stock'][sku]),
            'brand_rule': deepcopy(world['brand_rules'].get(variant['brand']))}


def material_fingerprint(result):
    variant = deepcopy(result.get('variant'))
    if not isinstance(variant, dict) or variant.get('description_excerpt_truncated'):
        return None
    variant.pop('description_excerpt_truncated', None)
    if variant.get('source_record_sha256') != digest(variant.get('source_record')):
        return None
    return digest({'variant': variant, 'stock': result.get('stock'), 'brand_rule': result.get('brand_rule')})


def review_sources(contract, current, observations, world):
    receipts, missing = [], []
    for sku in required_skus(contract, current):
        try:
            current_material = full_material(world, sku)
            expected = material_fingerprint(current_material)
        except (OrderError, KeyError):
            missing.append({'sku': sku, 'reason': 'current_source_data_invalid'})
            continue
        matches = [ident for ident, obs in observations.items() if obs.get('success') and
                   obs.get('tool') == 'read_variant' and material_fingerprint(obs.get('result', {})) == expected]
        if not matches:
            missing.append({'sku': sku, 'reason': 'complete_current_material_not_observed'})
        else:
            receipts.append({'sku': sku, 'observation_id': matches[-1],
                'material_sha256': expected, 'source_record_sha256': current_material['variant']['source_record_sha256'],
                'variant_version': current_material['variant']['version'],
                'stock_version': current_material['stock']['version'],
                'rule_version': (current_material['brand_rule'] or {}).get('version')})
    required = required_skus(contract, current)
    return {'version': VERSION, 'required_skus': required, 'receipts': receipts, 'missing': missing,
            'status': 'missing' if missing else 'ready' if required else 'not_required', 'passed': not missing,
            'scope': 'Complete recorded product material observed in this run and still current; '
                     'not proof that every generated sentence is entailed, or that simulated data are real.'}
