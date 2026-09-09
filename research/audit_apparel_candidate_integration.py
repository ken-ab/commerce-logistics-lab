"""Read-only evidence audit: no imports of the Agent, checker or model client."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / 'evidence/apparel_candidate_integration_v1'
PILOT = ROOT / 'evidence/apparel_candidate_agent_pilot_v1'
OUT = ROOT / 'evidence/apparel_candidate_integration_audit_20260909.json'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def micro(value):
    return int((Decimal(str(value)) * 1000000).to_integral_value(rounding=ROUND_CEILING))


def matches(variant, fields):
    return all(variant[k] == v for k, v in fields.items() if k in
               ('brand', 'color', 'size', 'style_id', 'category') and v is not None)


def main():
    if OUT.exists():
        raise FileExistsError('Completed audit exists; do not overwrite')
    checked_hashes = {}

    def verify(path, expected):
        actual = sha(path)
        assert actual == expected, str(path)
        checked_hashes[path.relative_to(ROOT).as_posix()] = actual

    for folder in (INTEGRATION, PILOT):
        registration, summary = read(folder / 'registration.json'), read(folder / 'summary.json')
        verify(folder / 'registration.json', summary['registration_sha256'])
        for name, value in registration['source_sha256'].items():
            verify(ROOT / name, value)
        for name, value in registration.get('initial_sha256', {}).items():
            verify(folder / name, value)
        for name, value in summary['result_sha256'].items():
            verify(folder / name, value)
    ir, si = read(INTEGRATION / 'registration.json'), read(INTEGRATION / 'summary.json')
    icases = read(INTEGRATION / 'cases.json')
    assert digest(icases) == ir['case_sha256']
    original_world = read(ROOT / 'data/apparel_fulfillment_v1.json')
    assert digest(original_world) == ir['world_sha256']
    variants = original_world['variants']
    assert len(variants) == 37 and len(icases) == 102
    assert len({c['group_id'] for c in icases}) == 34
    integration_counts = {mode: Counter() for mode in ('all', 'any')}
    integration_coverage = {mode: [] for mode in ('all', 'any')}
    for case in icases:
        expected = {sku for sku, v in variants.items() if matches(v, case['arguments'])}
        assert expected == set(case['expected_skus']) and expected
        path = INTEGRATION / 'runs' / (case['id'] + '.json')
        record = read(path)
        assert record['case'] == case
        assert record['registration_sha256'] == sha(INTEGRATION / 'registration.json')
        assert (path.with_suffix('.started')).exists()
        for mode, result in record['methods'].items():
            count = integration_counts[mode]
            count['queries'] += 1
            output = result['output']
            ids = [v['sku'] for v in output['variants']]
            assert len(ids) == len(set(ids)) and set(ids) <= expected
            assert not output['retrieval']['order_mutated']
            assert not output['retrieval']['substitution_approved']
            for v in output['variants']:
                source = variants[v['sku']]
                assert v['source_record_sha256'] == source['source_record_sha256'] == digest(source['source_record'])
                assert v['field_provenance'] == source['provenance']
                assert v['stock_catalog_units'] == 100
            top1 = bool(ids) and ids[0] in expected
            coverage = len(set(ids) & expected) / len(expected)
            assert top1 == result['top1_expected_candidate']
            assert coverage == result['expected_candidate_coverage']
            assert not result['scope_or_filter_errors']
            count['top1_expected_candidate'] += top1
            count['empty'] += not ids
            integration_coverage[mode].append(coverage)
            states = result['order_checks']
            assert set(states) == (set(ir['states']) if ids else set())
            assert result['unexecuted_order_states'] == (0 if ids else 5)
            count['order_states_unexecuted'] += result['unexecuted_order_states']
            for state, check in states.items():
                assert check['expected_status'] == ir['states'][state]
                assert check['actual_status'] == check['check']['status'] == ir['states'][state]
                assert check['passed']
                count['order_states_executed'] += 1
            for call in result['gpu_calls']:
                assert call['success'] and set(call['candidate_ids']) <= expected
                count['gpu_requests'] += 1
                count['gpu_query_product_pairs'] += len(call['candidate_ids'])
    for mode, count in integration_counts.items():
        assert all(value == si['methods'][mode][key] for key, value in count.items())
        assert statistics.mean(integration_coverage[mode]) == si['methods'][mode]['mean_expected_candidate_coverage']

    pr, sp = read(PILOT / 'registration.json'), read(PILOT / 'summary.json')
    verify(PILOT / 'cases.json', pr['cases_sha256'])
    cases = read(PILOT / 'cases.json')
    ids, paid_micro = set(), 0
    groups, case_records, requested_models, returned_models = {}, [], set(), set()
    with closing(sqlite3.connect((ROOT / 'evidence/api_budget.sqlite').as_uri() + '?mode=ro', uri=True)) as budget:
        budget.row_factory = sqlite3.Row
        for mode in ('all', 'any'):
            count, latency = Counter(), []
            for case in cases:
                folder = PILOT / 'runs' / (case['id'] + '-' + mode)
                result, execution, attempt = (read(folder / name) for name in ('result.json', 'execution.json', 'attempt.json'))
                verify(folder / 'execution.json', result['execution_sha256'])
                assert attempt['registration_sha256'] == sha(PILOT / 'registration.json')
                initial = PILOT / 'initial' / case['id']
                world = read(initial / 'world.json')
                seed = read(initial / 'seed.json')
                assert digest(execution['before']) == seed['view_digest']
                assert execution['before']['request'] == case['request']
                after = execution['after']
                assert after['request'] == case['request'] and len(after['selections']) == 1
                selected = after['selections'][0]
                variant = world['variants'][selected['sku']]
                line = case['request']['lines'][0]
                allowed = {sku for sku, v in world['variants'].items() if matches(v, line)}
                assert allowed == set(case['expected']['allowed_skus']) and selected['sku'] in allowed
                assert selected['line_id'] == line['line_id'] == 'item'
                # Independent, narrow arithmetic oracle for these four no-shipping cases.
                rules = world['brand_rules'][variant['brand']]
                quantity, pack = line['quantity'], variant['pieces_per_catalog_unit']
                assert line['unit'] == 'piece' and not case['request']['needs_shipping']
                assert case['request']['sales_region'] in rules['allowed_sales_regions']
                assert world['stock'][selected['sku']]['available_catalog_units'] * pack >= quantity
                status = ('unfulfillable' if quantity < rules['wholesale_minimum_pieces_per_sku'] else
                          'needs_clarification' if quantity % pack else 'ready')
                assert status == case['expected']['status'] == after['order_check']['status']
                decision = execution['report']['decision']
                assert decision['status'] == status
                assert decision['selection_snapshot'] == after['selections']
                assert set(decision['product_skus']) == {selected['sku']}
                assert not after['approved_substitutions'] and after['confirmation'] is None
                assert result['acceptance']['passed'] and all(result['acceptance']['checks'].values())
                assert execution['run_status'] == 'completed'
                proposals = after['proposals']
                assert len(proposals) == int(case['expected']['proposal'])
                for proposal in proposals:
                    assert proposal['state'] == 'pending' and proposal['version'] == 1
                    assert proposal['order_check']['status'] == 'ready'
                    assert proposal['route'] == {'segments': [], 'status': 'not_required', 'total_cost_cents': 0}
                    assert proposal['order_check']['quantities_pieces'] == {selected['sku']: quantity}
                    assert proposal['order_check']['quantities_catalog_units'] == {selected['sku']: quantity // pack}
                    assert proposal['request_revision'] == after['revision']
                with closing(sqlite3.connect((folder / 'operations.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
                    persisted = db.execute('SELECT request,selections,revision FROM drafts WHERE id=?', (after['id'],)).fetchone()
                    assert json.loads(persisted[0]) == after['request']
                    assert json.loads(persisted[1]) == after['selections'] and persisted[2] == after['revision']
                    assert db.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == 0
                    assert db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0] == 0
                    assert db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == len(proposals)
                    assert all(row[0] == 100 for row in db.execute('SELECT quantity FROM inventory'))
                tools = [t for t in execution['traces'] if t['kind'] == 'tool']
                assert all(t['success'] for t in tools)
                reads = {t['result']['variant']['sku'] for t in tools if t['tool'] == 'read_variant'}
                assert selected['sku'] in reads
                searches = [t for t in tools if t['tool'] == 'search_variants']
                for t in searches:
                    for candidate in t['result']['variants']:
                        assert candidate['sku'] in allowed
                        assert candidate['source_record_sha256'] == world['variants'][candidate['sku']]['source_record_sha256']
                    if t['result']['retrieval']['method'] == 'local_qwen_rerank':
                        count['gpu_requests'] += 1
                        count['gpu_query_product_pairs'] += t['result']['retrieval']['candidate_count']
                call_cost = 0
                for call_number, call in enumerate(execution['calls'], 1):
                    ident = call['budget_call_id']
                    assert ident not in ids and call['status'] == 'success'
                    ids.add(ident)
                    row = budget.execute('SELECT * FROM calls WHERE id=?', (ident,)).fetchone()
                    assert row and row['status'] == 'settled' and row['charged'] is not None
                    assert row['purpose'] == attempt['purpose'] + str(call_number)
                    assert json.loads(row['usage']) == call['usage']
                    assert row['charged'] == micro(call['estimated_cost_cny'])
                    requested_models.add(call['requested_model']); returned_models.add(call['returned_model'])
                    call_cost += row['charged']
                assert len(execution['calls']) == execution['model_calls'] == execution['successful_model_calls']
                assert call_cost == micro(execution['accounted_and_reserved_cny']) == micro(result['accounted_and_reserved_cny'])
                count.update(runs=1, accepted=1, model_calls=execution['model_calls'],
                             tool_calls=execution['tool_calls'], cost_micro_cny=call_cost, search_calls=len(searches))
                latency.append(execution['latency_seconds']); paid_micro += call_cost
                case_records.append({'case': case['id'], 'method': mode, 'independent_status': status,
                    'model_calls': execution['model_calls'], 'tool_calls': execution['tool_calls'],
                    'micro_cny': call_cost, 'latency_seconds': execution['latency_seconds'],
                    'queries': [{'query': t['arguments'].get('query'), 'returned': len(t['result']['variants']),
                                'retrieval_mode': t['result']['retrieval']['method']} for t in searches]})
            for key in ('runs', 'accepted', 'model_calls', 'tool_calls', 'search_calls'):
                assert count[key] == sp['groups'][mode][key]
            assert count['cost_micro_cny'] == micro(sp['groups'][mode]['cost_cny'])
            assert statistics.mean(latency) == sp['groups'][mode]['mean_latency_seconds']
            groups[mode] = dict(count) | {'mean_latency_seconds': statistics.mean(latency)}
        purpose_rows = budget.execute('SELECT id FROM calls WHERE purpose LIKE ?',
                                     ('commerce_apparel:candidate_adapter_pilot_%',)).fetchall()
        assert {row['id'] for row in purpose_rows} == ids
        total, rows = budget.execute('SELECT SUM(COALESCE(charged,reserved)),COUNT(*) FROM calls').fetchone()
    assert len(ids) == 40 and paid_micro == 291625
    assert sp['ledger_after']['micro_cny'] - sp['ledger_before']['micro_cny'] == paid_micro
    assert sp['ledger_after']['rows'] - sp['ledger_before']['rows'] == len(ids)
    assert (total, rows) == (sp['ledger_after']['micro_cny'], sp['ledger_after']['rows'])
    output = {'created_at': datetime.now(timezone.utc).isoformat(), 'passed': True,
        'scope': 'Post-hoc raw-file, source, persisted-state and exact cost audit; no repeated model inference or rescore.',
        'integration': {mode: dict(v) for mode, v in integration_counts.items()},
        'pilot': groups, 'pilot_cases': case_records, 'requested_models': sorted(requested_models),
        'returned_models': sorted(returned_models), 'unique_successful_paid_calls': len(ids),
        'paid_micro_cny': paid_micro, 'ledger_micro_cny': total, 'ledger_rows': rows,
        'verified_sha256': checked_hashes, 'new_model_calls_during_audit': 0,
        'audit_development_corrections': ['First audit attempt appended a second colon to the recorded purpose prefix '
            'and stopped at that assertion. Corrected to exact prefix plus call number; no experiment file changed or model rerun.'],
        'new_real_users': 0, 'deployment_changed': False,
        'limits': ['Known 37-variant merchant; repeated templates and four developer-authored orders.',
            'Source provenance checks verify recorded origins, not current marketplace stock or seller authority.',
            'Arithmetic oracle covers these no-shipping quantities/rules only, not all business states.',
            'Requested/returned model aliases retained; provider model weights are not independently verified.']}
    with OUT.open('x', encoding='utf-8') as file:
        file.write(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in output.items() if k not in ('verified_sha256', 'pilot_cases')}, indent=2))


if __name__ == '__main__':
    main()
