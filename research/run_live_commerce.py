"""Run and preserve a real model workflow, then verify a LOCAL simulation confirmation."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from commerce_lab.agent import run_sync
from commerce_lab.state import Store
from research.model_config import ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model')
    parser.add_argument('--task', default='Find a blue cotton t-shirt for men priced at no more than 50 USD. If a matching catalog item is available, prepare a cart with 2 units of that exact item. Then stage a local simulation order proposal to US, delivery within 10 days, shipping budget at most 30 USD. Check the product material from its catalog description. Do not claim real payment or shipment.')
    parser.add_argument('--confirm-simulation', action='store_true')
    args = parser.parse_args()
    store = Store()
    session = store.session()['id']
    result = run_sync(session, args.task, store=store, model=args.model)
    evidence = {'created_at': datetime.now(timezone.utc).isoformat(), 'task': args.task,
        'purpose': 'Development smoke run, not a frozen evaluation task or real customer', 'result': result}
    proposal = result.get('report', {}).get('proposal_id')
    if args.confirm_simulation and proposal:
        confirmed = store.confirm_proposal(session, proposal)
        again = store.confirm_proposal(session, proposal)
        fetched = store.get_order(session, confirmed['order_id'])
        evidence['host_confirmation'] = {'actor': 'development test harness', 'order': confirmed,
                                       'idempotent': confirmed == again, 'persisted_lookup_matches': confirmed == fetched}
    trace = store.run(session, result['run_id'])
    evidence['traces'] = trace['traces']
    target = ROOT / 'evidence' / ('live_commerce_' + result['run_id'] + '.json')
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'evidence_file': str(target), 'run_status': trace['status'],
        'report': result.get('report'), 'calls': result.get('model_calls'),
        'estimated_cost_cny': result.get('estimated_cost_cny'), 'error': result.get('error'),
        'simulation_order_confirmed': bool(evidence.get('host_confirmation'))}, ensure_ascii=False, indent=2))
    if trace['status'] != 'completed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
