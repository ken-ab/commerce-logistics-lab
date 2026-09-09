"""Small registered real-Agent development pilot of the candidate adapter."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics

from apparel_fulfillment.agent import MODEL
from apparel_fulfillment.agent_candidate_v4 import CandidateSearchAgent
from apparel_fulfillment.data import load_world, digest
from apparel_fulfillment.store import ApparelStore
from research.provider_gate import ProviderGate, guarded_business_client
from research.apparel_candidate_integration import ledger

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/apparel_candidate_agent_pilot_v1'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def save(path, value):
    with path.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def prepare():
    if OUT.exists():
        raise FileExistsError('Pilot already registered')
    integration = read(ROOT / 'evidence/apparel_candidate_integration_v1/summary.json')
    assert integration['query_method_runs'] == 204
    world = load_world()
    for row in world['stock'].values(): row['available_catalog_units'] = 100
    definitions = [('gt-ready', 'Goodthreads', 'black', 'M', 20, 'ready', True),
        ('ae-ready', 'Amazon Essentials', 'light blue', 'M', 22, 'ready', True),
        ('gt-moq', 'Goodthreads', 'red', 'M', 8, 'unfulfillable', False),
        ('ae-pack', 'Amazon Essentials', 'black', 'M', 21, 'needs_clarification', False)]
    cases = []
    for ident, brand, color, size, quantity, status, proposal in definitions:
        request = {'sales_region': 'DE', 'wholesale': True, 'needs_shipping': False,
            'lines': [{'line_id': 'item', 'brand': brand, 'color': color, 'size': size,
                       'category': 't_shirt', 'quantity': quantity, 'unit': 'piece'}]}
        allowed = sorted(sku for sku, v in world['variants'].items() if
            (v['brand'], v['color'], v['size'], v['category']) == (brand, color, size, 't_shirt'))
        assert allowed
        task = ('我想订一批cotton tee。按已填品牌、颜色和尺码找合适变体，读取来源后选入item；'
                '按原数量和单位核验，不自行放宽要求。')
        task += '合格后准备不需要运输的订单提案，附依据，等我单独确认。' if proposal else (
            '本次只把候选选入供审阅，核验并说明阻断问题，不能改数量或单位，不准备提案或确认订单。')
        cases.append({'id': ident, 'request': request, 'task': task,
            'contract': {'mode': 'prepare_proposal'} if proposal else {'mode': 'stage_candidate', 'line_id': 'item'},
            'expected': {'allowed_skus': allowed, 'status': status, 'proposal': proposal},
            'now': '2026-09-24T02:00:00+00:00'})
    OUT.mkdir(); (OUT / 'initial').mkdir(); (OUT / 'runs').mkdir()
    save(OUT / 'cases.json', cases)
    initial_sha = {}
    for case in cases:
        folder = OUT / 'initial' / case['id']; folder.mkdir()
        store = ApparelStore(folder / 'operations.sqlite', world=deepcopy(world))
        draft = store.create_draft('pilot', case['request'])
        save(folder / 'world.json', world)
        save(folder / 'seed.json', {'draft_id': draft['id'], 'view_digest': digest(store.view('pilot', draft['id']))})
        for path in folder.iterdir(): initial_sha[path.relative_to(OUT).as_posix()] = sha(path)
    paths = ['research/apparel_candidate_agent_pilot.py', 'research/APPAREL_CANDIDATE_AGENT_PILOT_PROTOCOL.md',
        'apparel_fulfillment/agent_candidate_v4.py', 'apparel_fulfillment/candidate_search.py',
        'apparel_fulfillment/agent.py', 'apparel_fulfillment/agent_evidence_v2.py',
        'apparel_fulfillment/agent_state_v3.py', 'apparel_fulfillment/action_contract.py',
        'apparel_fulfillment/store.py', 'apparel_fulfillment/orders.py', 'research/provider_gate.py',
        'research/model_client.py', 'research/rate_card.json', 'research/tls_transport.py',
        'delivery_budget.py', 'delivery_budget_policy.json', 'evidence/apparel_candidate_integration_v1/summary.json']
    save(OUT / 'registration.json', {'registered_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'Four known-catalogue development orders, two candidate policies, one fixed generative model.',
        'model': MODEL, 'cases_sha256': sha(OUT / 'cases.json'), 'runs': 8,
        'source_sha256': {p: sha(ROOT / p) for p in paths}, 'initial_sha256': initial_sha,
        'ledger_before': ledger(), 'estimated_cny': [1, 3], 'project_budget_cny': 480,
        'new_model_selection_runs': 0, 'deployment_changed': False})
    print(json.dumps({'registered': 8, 'cases': 4, 'model': MODEL}))


def assess(case, result):
    after = result['after']
    report = result.get('report') or {}
    decision = report.get('decision') or {}
    allowed = set(case['expected']['allowed_skus'])
    selected = after['selections']
    ids = [p['sku'] for p in selected]
    observations = [t for t in result['traces'] if t['kind'] == 'tool' and t['success']]
    source_read = {t['result']['variant']['sku'] for t in observations if t['tool'] == 'read_variant'}
    snapshots = decision.get('selection_snapshot')
    checks = {
        'run_completed': result['run_status'] == 'completed',
        'selected_exact_candidate': len(selected) == 1 and selected[0]['line_id'] == 'item' and ids[0] in allowed,
        'order_status': after['order_check']['status'] == case['expected']['status'],
        'decision_status': decision.get('status') == case['expected']['status'],
        'source_read_for_selection': bool(ids) and set(ids) <= source_read,
        'report_matches_actual_selection': snapshots == selected and set(decision.get('product_skus', [])) == set(ids),
        'proposal_presence': bool(after['proposals']) == case['expected']['proposal'],
        'request_preserved': after['request'] == case['request'],
        'no_approval_or_confirmation': not after['approved_substitutions'] and after['confirmation'] is None,
        'valid_grounding': bool(report.get('grounding', {}).get('supported')) and not report.get('grounding', {}).get('invalid')}
    searches = [{'arguments': t['arguments'], 'retrieval': t['result']['retrieval'],
                 'returned_skus': [v['sku'] for v in t['result']['variants']]} for t in observations if t['tool'] == 'search_variants']
    return {'checks': checks, 'passed': all(checks.values()), 'searches': searches,
        'scope': 'Predefined development acceptance checks, not an autonomous-agent benchmark score.'}


def run():
    if (OUT / 'summary.json').exists(): raise FileExistsError('Pilot is complete')
    registration = read(OUT / 'registration.json')
    assert all(sha(ROOT / name) == value for name, value in registration['source_sha256'].items())
    assert sha(OUT / 'cases.json') == registration['cases_sha256']
    assert all(sha(OUT / name) == value for name, value in registration['initial_sha256'].items())
    gate = ProviderGate(ROOT / 'evidence/provider_availability.sqlite')
    gate.check('aihubmix')
    cases = read(OUT / 'cases.json')
    records = []
    for index, case in enumerate(cases):
        for mode in (('all', 'any') if index % 2 == 0 else ('any', 'all')):
            folder = OUT / 'runs' / (case['id'] + '-' + mode)
            if (folder / 'result.json').exists():
                records.append(read(folder / 'result.json')); continue
            if folder.exists(): raise RuntimeError('Partial pilot attempt must not be silently repeated')
            client = guarded_business_client(gate)
            client.ensure_available(MODEL)
            folder.mkdir()
            initial = OUT / 'initial' / case['id']
            with closing(sqlite3.connect((initial / 'operations.sqlite').as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(folder / 'operations.sqlite')) as destination: source.backup(destination)
            seed, world = read(initial / 'seed.json'), read(initial / 'world.json')
            store = ApparelStore(folder / 'operations.sqlite', world=world)
            assert digest(store.view('pilot', seed['draft_id'])) == seed['view_digest']
            agent = CandidateSearchAgent(store, 'pilot', seed['draft_id'], client=client, arm='single',
                now=datetime.fromisoformat(case['now']), contract=case['contract'], candidate_match_mode=mode,
                phase='candidate_adapter_pilot_' + mode)
            save(folder / 'attempt.json', {'case_id': case['id'], 'match_mode': mode, 'purpose': agent.purpose,
                'registration_sha256': sha(OUT / 'registration.json')})
            result = agent.run(case['task'])
            save(folder / 'execution.json', result)
            evaluation = assess(case, result)
            record = {'case_id': case['id'], 'match_mode': mode, 'run_status': result['run_status'],
                'acceptance': evaluation, 'model_calls': result['model_calls'], 'tool_calls': result['tool_calls'],
                'accounted_and_reserved_cny': result['accounted_and_reserved_cny'],
                'latency_seconds': result['latency_seconds'], 'execution_sha256': sha(folder / 'execution.json')}
            save(folder / 'result.json', record); records.append(record)
            print(json.dumps({k: record[k] for k in ('case_id', 'match_mode', 'run_status', 'model_calls', 'accounted_and_reserved_cny')} |
                             {'acceptance_passed': evaluation['passed']}, ensure_ascii=False), flush=True)
    summary = {'completed_at': datetime.now(timezone.utc).isoformat(), 'runs': len(records), 'cases': 4,
        'registration_sha256': sha(OUT / 'registration.json'), 'groups': {},
        'result_sha256': {p.relative_to(OUT).as_posix(): sha(p) for p in (OUT / 'runs').glob('*/result.json')},
        'ledger_before': registration['ledger_before'], 'ledger_after': ledger(),
        'new_model_selection_runs': 0, 'new_real_users': 0, 'deployment_changed': False}
    for mode in ('all', 'any'):
        selected = [r for r in records if r['match_mode'] == mode]
        summary['groups'][mode] = {'runs': len(selected), 'accepted': sum(r['acceptance']['passed'] for r in selected),
            'model_calls': sum(r['model_calls'] for r in selected), 'tool_calls': sum(r['tool_calls'] for r in selected),
            'cost_cny': sum(float(r['accounted_and_reserved_cny']) for r in selected),
            'mean_latency_seconds': statistics.mean(r['latency_seconds'] for r in selected),
            'search_calls': sum(len(r['acceptance']['searches']) for r in selected)}
    assert len(records) == 8
    save(OUT / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('phase', choices=['prepare', 'run'])
    {'prepare': prepare, 'run': run}[parser.parse_args().phase]()
