"""Export one passing deterministic acceptance trace, without host metadata."""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def read_acceptance(path):
    root = ET.parse(path).getroot()
    suites = list(root.iter('testsuite'))
    cases = list(root.iter('testcase'))
    assert len(suites) == len(cases) == 1, 'Expected this single acceptance scenario'
    suite, case = suites[0], cases[0]
    assert suite.attrib['tests'] == '1'
    assert all(suite.attrib[k] == '0' for k in ('errors', 'failures', 'skipped'))
    assert not any(case.find(k) is not None for k in ('error', 'failure', 'skipped'))
    assert case.attrib['name'] == 'test_substitute_cancel_revise_stock_change_and_confirm_once'
    props = {p.attrib['name']: p.attrib['value'] for p in case.iter('property')}
    trace = json.loads(props['http_trace_json'])
    milestones = json.loads(props['milestones_json'])
    assert len(trace) == 19 and len(milestones) == 6
    assert props['model_calls'] == props['gpu_calls'] == '0'

    proposals = [trace[i]['response'] for i in (7, 11, 13)]
    records = trace[16]['response']
    confirmed, repeated = [trace[i]['response'] for i in (14, 15)]
    assert confirmed['confirmation_id'] == repeated['confirmation_id']
    assert repeated['idempotent_replay'] is True
    assert [p['version'] for p in proposals] == [1, 2, 3]
    assert records['draft']['request'] == trace[0]['input']['order']
    assert [p['state'] for p in records['draft']['proposals']] == ['superseded', 'superseded', 'confirmed']
    assert all(p['route']['shipping_constraints'] == trace[0]['input']['order']['shipping'] for p in proposals)

    # Only generated identities are replaced. Business values, digests, errors,
    # timestamps and the order of all requests remain available for comparison.
    identities = {
        trace[0]['response']['id']: 'draft-1',
        trace[0]['response']['owner']: 'test-session-1',
        confirmed['confirmation_id']: 'confirmation-1',
        **{p['proposal_id']: f'proposal-{p["version"]}' for p in proposals},
    }
    encoded = json.dumps(trace, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    for value, replacement in sorted(identities.items(), key=lambda pair: -len(pair[0])):
        encoded = encoded.replace(value, replacement)
    normalized = json.loads(encoded)
    confirmations = [t for t in records['traces'] if t['kind'] == 'confirm_simulation']
    stock = records['draft']['order_check']['source_versions']['B']['available_catalog_units']
    assert len(confirmations) == 1 and stock == 0
    assert sum(t['status_code'] == 409 for t in trace) == 3
    return {
        'schema_version': 1,
        'scope': props['scenario'],
        'source_xml_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'execution': {
            'test': case.attrib['name'], 'timestamp': suite.attrib['timestamp'],
            'suite_seconds': float(suite.attrib['time']),
            'tests': 1, 'errors': 0, 'failures': 0, 'skipped': 0,
        },
        'business_clock': props['business_time'],
        'independent_scenarios': 1,
        'http_requests': len(trace),
        'expected_http_409_responses': 3,
        'model_calls': 0, 'gpu_calls': 0, 'new_model_cost_cny': 0,
        'real_customers': 0,
        'milestones': milestones,
        'proposal_versions': [{
            'version': p['version'],
            'air_departure': next(s['departure_at'] for s in p['route']['segments'] if s['mode'] == 'air'),
            'arrival_at': p['route']['arrival_at'],
            'total_cost_cents': p['route']['total_cost_cents'],
            'currency': p['route']['currency'],
            'selected_quantities': p['order_check']['quantities_catalog_units'],
        } for p in proposals],
        'confirmation_trace_events': len(confirmations),
        'final_available_units_B': stock,
        'normalization': 'Replace only generated draft, owner-session, proposal and confirmation IDs with stable aliases; omit XML hostname and local source path.',
        'normalized_http_trace_sha256': hashlib.sha256(encoded.encode('utf-8')).hexdigest(),
        'normalized_http_trace': normalized,
        'limits': [
            'One designed scenario; neither 19 independent cases nor a model success rate.',
            'All brands, stock, rules, transport rates and departures are test fixtures.',
            'Two explicit simulated stock updates are made by the test outside the HTTP trace.',
            'Model jobs are forbidden in this test; real model performance remains a separate experiment.',
            'Host test duration is not agent or model latency.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('xml', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = read_acceptance(args.xml)
    # Preserve earlier evidence rather than overwriting a prior execution.
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({k: result[k] for k in ('independent_scenarios', 'http_requests', 'model_calls', 'normalized_http_trace_sha256')}))


if __name__ == '__main__':
    main()
