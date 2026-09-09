"""Registered development integration: source-scoped candidates + order checks.

GPU ranking is real when multiple candidates remain; no generative/API client.
Synthetic query/state constructions are not independent business users or a
held-out public benchmark. Completed records are never silently overwritten.
"""
from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import time
from urllib.request import ProxyHandler, build_opener

from apparel_fulfillment.candidate_search import search_variants
from apparel_fulfillment.data import load_world, digest
from apparel_fulfillment.orders import check_order
from commerce_lab.retrieval import request_scores

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/apparel_candidate_integration_v1'
FORMS = [('literal', 'cotton t-shirt'), ('reordered', 'shirt cotton'), ('synonym', 'cotton tee')]
STATES = {'normal': 'ready', 'stock_zero': 'unfulfillable', 'missing_unit': 'needs_clarification',
          'restricted_region': 'unfulfillable', 'changed_size': 'needs_clarification'}


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def write(path, value):
    with path.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def ledger():
    with closing(sqlite3.connect((ROOT / 'evidence/api_budget.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        value, rows = db.execute('SELECT SUM(COALESCE(charged,reserved)),COUNT(*) FROM calls').fetchone()
    return {'micro_cny': value, 'rows': rows}


def register():
    if OUT.exists():
        raise FileExistsError('Integration registration exists; inspect it instead of restarting')
    world = load_world()
    profiles = defaultdict(list)
    for sku, v in world['variants'].items():
        assert v['category'] == 't_shirt'
        assert 'cotton' in (v['source_record']['title'] + ' ' + v['source_record']['description']).lower()
        assert world['brand_rules'][v['brand']]['wholesale_minimum_pieces_per_sku'] <= 20
        assert 'DE' in world['brand_rules'][v['brand']]['allowed_sales_regions']
        assert 'JP' not in world['brand_rules'][v['brand']]['allowed_sales_regions']
        profiles[tuple(v[k] for k in ('brand', 'color', 'size'))].append(sku)
    cases = []
    for values, skus in sorted(profiles.items()):
        filters = dict(zip(('brand', 'color', 'size'), values))
        group_id = digest(filters)[:20]
        request = {'sales_region': 'DE', 'wholesale': True, 'needs_shipping': False,
            'lines': [{'line_id': 'one', 'quantity': 20, 'unit': 'piece', 'category': 't_shirt', **filters}]}
        for form, query in FORMS:
            cases.append({'id': group_id + '-' + form, 'group_id': group_id, 'query_form': form,
                'arguments': {'query': query, **filters}, 'request': request,
                'expected_skus': sorted(skus), 'expected_state_status': STATES})
    paths = ['research/apparel_candidate_integration.py', 'research/APPAREL_CANDIDATE_INTEGRATION_PROTOCOL.md',
        'apparel_fulfillment/candidate_search.py', 'apparel_fulfillment/agent_candidate_v4.py',
        'apparel_fulfillment/agent_state_v3.py', 'apparel_fulfillment/orders.py', 'apparel_fulfillment/data.py',
        'commerce_lab/retrieval.py', 'serving/reranker.py', 'data/apparel_fulfillment_v1.json',
        'tests/test_apparel_candidate_search.py']
    registration = {'registered_at': now(), 'version': 'apparel-candidate-integration-v1',
        'scope': 'Development integration on known apparel sources, not autonomous Agent or unseen benchmark success.',
        'merchant_variants': len(world['variants']), 'constraint_groups': len(profiles), 'queries': len(cases),
        'methods': ['all', 'any'], 'query_forms': FORMS, 'states': STATES,
        'case_sha256': digest(cases), 'source_sha256': {p: sha(ROOT / p) for p in paths},
        'world_sha256': digest(world), 'model': 'Qwen/Qwen3-Reranker-0.6B',
        'local_model_policy': 'Only multiple lexical candidates call the existing GPU scorer; zero/one bypasses it.',
        'execution': 'Fixed case order; alternate method order by index; sequential calls, no hidden retries.',
        'expected_labels': 'Explicit metadata-equality candidate sets; independent constructed business-state expectations.',
        'release_authority': False, 'new_paid_calls': 0, 'ledger_before': ledger()}
    OUT.mkdir(); (OUT / 'runs').mkdir()
    write(OUT / 'cases.json', cases)
    write(OUT / 'registration.json', registration)
    print(json.dumps({k: registration[k] for k in ('merchant_variants', 'constraint_groups', 'queries', 'new_paid_calls')}, indent=2))


def verify():
    r = read(OUT / 'registration.json')
    assert all(sha(ROOT / p) == expected for p, expected in r['source_sha256'].items())
    assert digest(read(OUT / 'cases.json')) == r['case_sha256']
    return r


def run():
    r = verify()
    if (OUT / 'summary.json').exists():
        raise FileExistsError('Completed integration is frozen')
    with build_opener(ProxyHandler({})).open('http://127.0.0.1:5175/health', timeout=8) as response:
        health = json.load(response)
    assert health == {'ready': True, 'model': r['model'], 'instruction': 'product'}
    world = load_world()
    for item in world['stock'].values():
        item['available_catalog_units'] = 100
    frozen_world = digest(world)
    all_results = []
    cases = read(OUT / 'cases.json')
    for index, case in enumerate(cases):
        path = OUT / 'runs' / (case['id'] + '.json')
        if path.exists():
            previous = read(path)
            assert previous['case'] == case and previous['registration_sha256'] == sha(OUT / 'registration.json')
            all_results.append(previous)
            continue
        # A start marker prevents repeating a partly executed GPU pair after interruption.
        attempt_path = OUT / 'runs' / (case['id'] + '.started')
        if attempt_path.exists():
            raise RuntimeError('Incomplete recorded attempt requires explicit recovery, not an automatic rerun')
        write(attempt_path, {'started_at': now(), 'case_id': case['id']})
        methods = {}
        for method in (('all', 'any') if index % 2 == 0 else ('any', 'all')):
            calls = []
            def recorded_scorer(query, rows):
                call = {'query': query, 'candidate_ids': [row['id'] for row in rows], 'started_at': now()}
                calls.append(call)
                start = time.monotonic()
                try:
                    values = request_scores(query, rows)
                    call.update(success=True, scores=values)
                    return values
                except Exception as error:
                    call.update(success=False, error_type=type(error).__name__)
                    raise
                finally:
                    call['wall_seconds'] = time.monotonic() - start
            start = time.monotonic()
            output = search_variants(world, deepcopy(case['arguments']), match_mode=method, scorer=recorded_scorer)
            retrieval_seconds = time.monotonic() - start
            ids = [v['sku'] for v in output['variants']]
            expected = set(case['expected_skus'])
            scope_errors = [sku for sku in ids if sku not in world['variants'] or sku not in expected]
            checks = {}
            if ids:
                for state, status in STATES.items():
                    state_world, request = deepcopy(world), deepcopy(case['request'])
                    if state == 'stock_zero':
                        state_world['stock'][ids[0]]['available_catalog_units'] = 0
                    elif state == 'missing_unit': request['lines'][0]['unit'] = None
                    elif state == 'restricted_region': request['sales_region'] = 'JP'
                    elif state == 'changed_size':
                        request['lines'][0]['size'] = 'XS' if world['variants'][ids[0]]['size'] != 'XS' else 'XXL'
                    result = check_order(request, [{'line_id': 'one', 'sku': ids[0]}], state_world)
                    checks[state] = {'expected_status': status, 'actual_status': result['status'],
                                     'passed': result['status'] == status, 'check': result}
            assert digest(world) == frozen_world
            methods[method] = {'output': output, 'gpu_calls': calls, 'retrieval_seconds': retrieval_seconds,
                'top1_expected_candidate': bool(ids) and ids[0] in expected,
                'expected_candidate_coverage': len(set(ids) & expected) / len(expected),
                'scope_or_filter_errors': scope_errors, 'order_checks': checks,
                'unexecuted_order_states': 0 if ids else len(STATES)}
        result = {'recorded_at': now(), 'registration_sha256': sha(OUT / 'registration.json'), 'case': case, 'methods': methods}
        write(path, result)
        all_results.append(result)
        if (index + 1) % 15 == 0 or index + 1 == len(cases):
            print(json.dumps({'completed_queries': index + 1, 'total': len(cases)}, ensure_ascii=False), flush=True)
    aggregate = {}
    for method in ('all', 'any'):
        values = [r['methods'][method] for r in all_results]
        latencies = sorted(v['retrieval_seconds'] for v in values)
        gpu = [c for v in values for c in v['gpu_calls']]
        by_form = {}
        for form, _ in FORMS:
            selected = [r['methods'][method] for r in all_results if r['case']['query_form'] == form]
            by_form[form] = {'queries': len(selected), 'top1_expected_candidate': sum(v['top1_expected_candidate'] for v in selected)}
        aggregate[method] = {'queries': len(values), 'top1_expected_candidate': sum(v['top1_expected_candidate'] for v in values),
            'empty': sum(not v['output']['variants'] for v in values),
            'mean_expected_candidate_coverage': statistics.mean(v['expected_candidate_coverage'] for v in values),
            'scope_or_filter_errors': sum(len(v['scope_or_filter_errors']) for v in values),
            'mean_retrieval_seconds': statistics.mean(latencies), 'p95_retrieval_seconds': latencies[(95 * len(latencies) + 99) // 100 - 1],
            'gpu_requests': len(gpu), 'gpu_request_success': sum(c['success'] for c in gpu),
            'gpu_query_product_pairs': sum(len(c['candidate_ids']) for c in gpu),
            'ranking_modes': dict(Counter(v['output']['retrieval']['method'] for v in values)),
            'order_states_executed': sum(len(v['order_checks']) for v in values),
            'order_states_passed': sum(c['passed'] for v in values for c in v['order_checks'].values()),
            'order_states_unexecuted': sum(v['unexecuted_order_states'] for v in values), 'by_query_form': by_form}
    assert ledger() == r['ledger_before']
    raw_paths = sorted((OUT / 'runs').glob('*.json'))
    assert len(raw_paths) == len(cases)
    summary = {'completed_at': now(), 'registration_sha256': sha(OUT / 'registration.json'),
        'source_scope': r['scope'], 'merchant_variants': r['merchant_variants'], 'constraint_groups': r['constraint_groups'],
        'query_cases': len(cases), 'query_method_runs': 2 * len(cases), 'methods': aggregate,
        'result_sha256': {p.relative_to(OUT).as_posix(): sha(p) for p in raw_paths},
        'new_paid_calls': 0, 'new_generative_agent_runs': 0, 'new_real_users': 0, 'deployment_changed': False,
        'ledger_after': ledger(), 'health_before': health,
        'limits': ['Developer-authored repeated query templates on known source records; not unseen natural requests.',
            'Exact structured filters largely determine candidate eligibility, so top-1 is a tool-contract check, not LLM accuracy.',
            'State checks are deterministic checker executions, not autonomous Agent completion or real merchant outcomes.',
            'Any GPU errors and bypasses remain separately counted. Single-pass timing is not an SLA or causal speedup.']}
    write(OUT / 'summary.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k != 'result_sha256'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['register', 'run'])
    {'register': register, 'run': run}[parser.parse_args().phase]()
