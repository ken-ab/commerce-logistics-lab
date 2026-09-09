"""Pre-run scenario feasibility audit. No model calls and no model-quality claims."""
from contextlib import closing
import json
import argparse

from apparel_fulfillment.data import ROOT, digest
from apparel_fulfillment.route_audit import audit_route
from research.apparel_cases import setup


def audit_case(case, directory):
    store, draft_id, now = setup(case, directory / 'operations.sqlite')
    before = store.view('evaluation', draft_id)
    view = before
    expected = case['expected']
    old_audit = None
    if case['scenario_kind']:
        old = before['proposals'][-1]
        old_audit = store.assess('evaluation', draft_id, old['proposal_id'], now=now)
        assert old_audit['valid'] == expected['old_valid'], 'Old-route validity construction failed'
    if expected.get('must_read_alternatives') or case['initial_sku'] is None:
        sku = expected['selected_skus'][0]
        view = store.select('evaluation', draft_id, [{'line_id': line['line_id'], 'sku': sku} for line in case['request']['lines']], expected_revision=view['revision'])
    if expected.get('issue'):
        assert expected['issue'] in {i['code'] for i in view['order_check']['issues']}, 'Constructed issue missing'
    elif case['family'] != 'product_info':
        assert view['order_check']['status'] == 'ready', 'Constructed order cannot be checked ready'
    route_check = None
    if expected['proposal'] in {'new', 'revision', 'infeasible'}:
        proposal = store.propose('evaluation', draft_id, expected_revision=view['revision'], now=now)
        assert proposal['route']['status'] == ('infeasible' if expected['proposal'] == 'infeasible' else 'planned' if case['request']['needs_shipping'] else 'not_required')
        if proposal['route']['status'] != 'infeasible':
            route_check = audit_route(proposal['order_check'], case['request'].get('shipping'), proposal['route'],
                                      store.base_world, store.corridor, events=store.transport_events(), now=now)
            assert route_check['passed'], 'Independent route audit failed'
        else:
            choices = proposal['route']['adjustment_options']
            assert choices and all(c['requires_user_choice'] for c in choices), 'Missing explicit relaxation options'
    after = store.view('evaluation', draft_id)
    record = {'case_id': case['id'], 'family': case['family'], 'expected': expected, 'before': before, 'after': after,
              'events': store.transport_events(), 'old_route_audit': old_audit, 'new_route_audit': route_check,
              'traces': store.traces('evaluation', draft_id), 'source_snapshot_digest': digest(store.base_world),
              'passed': True, 'notice': 'Deterministic fixture audit only; no model accuracy is measured.'}
    (directory / 'audit.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'case_id': case['id'], 'partition': case['partition'], 'family': case['family'], 'passed': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', default='v2')
    args = parser.parse_args()
    if not args.label.replace('_', '').isalnum(): raise ValueError('Simple audit label required')
    folder = ROOT / ('evidence/apparel_fixture_audit_' + args.label)
    if folder.exists(): raise FileExistsError('Preserve the existing fixture audit')
    folder.mkdir(parents=True)
    rows = []
    for partition in ('validation', 'test'):
        data = json.loads((ROOT / f'data/apparel_cases_{partition}_v1.json').read_text(encoding='utf-8'))
        for case in data['cases']:
            rows.append(audit_case(case, folder / case['id']))
    (folder / 'summary.json').write_text(json.dumps({'cases': len(rows), 'passed': sum(r['passed'] for r in rows), 'rows': rows}, indent=2), encoding='utf-8')
    print(json.dumps({'audited': len(rows), 'passed': sum(r['passed'] for r in rows)}))
