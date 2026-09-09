"""Pre-register and execute the full expanded fixed-business strategy comparison."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from itertools import permutations
import argparse
import json
from pathlib import Path
import random
import statistics

from apparel_fulfillment.agent import MODEL
from apparel_fulfillment.agent_source_v5 import SourceReviewAgent
from apparel_fulfillment.data import digest
from apparel_fulfillment.transport import instant
from research.apparel_expansion_cases import OWNER, FAMILIES, cases, setup, fixture_check
from research.apparel_expansion_evaluation import evaluate
from research.apparel_candidate_validation import clone, read, save, sha
from research.apparel_candidate_integration import ledger
from research.provider_gate import ProviderGate, guarded_business_client

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/apparel_expansion_study_v1'
ARMS = ('single', 'coordinator', 'on_demand')
SEED = 26090972


def prepare():
    if OUT.exists():
        raise FileExistsError('Preserve an existing registration or partial preflight')
    source_audit = read(ROOT / 'evidence/apparel_expansion_sources_v1/audit.json')
    assert source_audit['passed'] and source_audit['raw_rows_reconciled'] == 240
    assert all(sha(ROOT / name) == value for name, value in source_audit['hashes'].items())
    rows = cases()
    assert len(rows) == 72
    OUT.mkdir()
    (OUT / 'runs').mkdir()
    (OUT / 'fixtures').mkdir()
    save(OUT / 'cases.json', rows)
    checks, initial_sha = [], {}
    for case in rows:
        initial = OUT / 'initial' / case['id']
        store, seed = setup(case, initial)
        checks.append(fixture_check(case, initial, seed, OUT / 'fixtures' / (case['id'] + '.sqlite')))
        for path in initial.iterdir():
            initial_sha[path.relative_to(OUT).as_posix()] = sha(path)
    save(OUT / 'fixture_checks.json', checks)
    order = [c['id'] for c in rows]
    random.Random(SEED).shuffle(order)
    schedules = list(permutations(ARMS))
    jobs = [(ident, arm) for index, ident in enumerate(order) for arm in schedules[index % len(schedules)]]
    previous = read(ROOT / 'evidence/apparel_source_validation_v1/registration.json')
    sources = set(previous['source_sha256']) | {
        'research/apparel_expansion_data.py', 'research/audit_apparel_expansion_sources.py',
        'research/apparel_expansion_cases.py', 'research/apparel_expansion_evaluation.py', 'research/apparel_expansion_study.py',
        'tests/test_apparel_expansion_evaluation.py',
        'research/APPAREL_EXPANSION_PROTOCOL.md', 'research/audit_apparel_candidate_validation.py',
        'data/apparel_fulfillment_expansion_v1.json', 'evidence/apparel_expansion_sources_v1/manifest.json',
        'evidence/apparel_expansion_sources_v1/raw_records.json', 'evidence/apparel_expansion_sources_v1/audit.json'}
    targets = {s['sku'] for c in rows for s in c['initial_selections']}
    targets.update(c['contract']['product_sku'] for c in rows if c['contract']['mode'] == 'inspect_product')
    targets.update(c['approve_sku'] for c in rows if c['approve_sku'])
    save(OUT / 'registration.json', {'registered_at': datetime.now(timezone.utc).isoformat(),
        'cases': 72, 'families': len(FAMILIES), 'runs': 216, 'model': MODEL, 'arms': ARMS,
        'jobs': jobs, 'seed': SEED, 'arm_position_balance': {arm: [sum(row[pos] == arm for row in schedules) * 12 for pos in range(3)] for arm in ARMS},
        'catalog_variants': 277, 'new_catalog_variants': 240, 'distinct_initial_inspection_or_approved_targets': len(targets),
        'cases_sha256': sha(OUT / 'cases.json'), 'fixture_sha256': sha(OUT / 'fixture_checks.json'),
        'source_sha256': {name: sha(ROOT / name) for name in sorted(sources)}, 'initial_sha256': initial_sha,
        'ledger_before': ledger(), 'estimated_cny': [8, 25], 'project_budget_cny': 480, 'model_selection_calls': 0,
        'deployment_changed': False, 'scope': 'New apparel research snapshot and developer-authored composition states. Historical public data, not unknown to pretraining or independently collected user tasks.'})
    print(json.dumps({'registered_cases': 72, 'registered_runs': 216, 'free_fixture_checks': len(checks),
                      'distinct_initial_inspection_or_approved_targets': len(targets), 'ledger_before': ledger()}, ensure_ascii=False))


def run():
    if (OUT / 'summary.json').exists():
        raise FileExistsError('Completed results are frozen')
    registration = read(OUT / 'registration.json')
    assert all(sha(ROOT / name) == value for name, value in registration['source_sha256'].items())
    assert all(sha(OUT / name) == value for name, value in registration['initial_sha256'].items())
    assert sha(OUT / 'cases.json') == registration['cases_sha256']
    assert sha(OUT / 'fixture_checks.json') == registration['fixture_sha256']
    rows = {c['id']: c for c in read(OUT / 'cases.json')}
    records = []
    gate = ProviderGate(ROOT / 'evidence/provider_availability.sqlite')
    for ident, arm in registration['jobs']:
        folder = OUT / 'runs' / (ident + '-' + arm)
        if (folder / 'result.json').exists():
            records.append(read(folder / 'result.json'))
            continue
        if folder.exists():
            raise RuntimeError('A partial attempt needs inspection; no silent repeat')
        client = guarded_business_client(gate)
        client.ensure_available(MODEL)
        folder.mkdir()
        case = rows[ident]
        initial = OUT / 'initial' / ident
        seed = read(initial / 'seed.json')
        store = clone(initial, folder / 'operations.sqlite')
        assert digest(store.view(OWNER, seed['draft_id'])) == seed['view_digest']
        agent = SourceReviewAgent(store, OWNER, seed['draft_id'], client=client, arm=arm, now=instant(case['now']),
                                 contract=case['contract'], phase='expanded_apparel_v1_' + arm)
        save(folder / 'attempt.json', {'case_id': ident, 'arm': arm, 'purpose': agent.purpose,
                                      'registration_sha256': sha(OUT / 'registration.json')})
        execution = agent.run(case['task'])
        save(folder / 'execution.json', execution)
        measured = evaluate(case, execution, store, seed)
        record = {'case_id': ident, 'family': case['family'], 'arm': arm, 'evaluation': measured,
                  'execution_sha256': sha(folder / 'execution.json'), **{k: execution[k] for k in
                    ('run_status', 'error_type', 'model_calls', 'successful_model_calls', 'tool_calls', 'successful_tool_calls',
                     'input_tokens', 'output_tokens', 'accounted_and_reserved_cny', 'latency_seconds', 'delegations')}}
        save(folder / 'result.json', record)
        records.append(record)
        print(json.dumps({'completed': len(records), 'total': 216, 'case': ident, 'family': case['family'], 'arm': arm,
                          'passed': measured['passed'], 'failures': measured['failures'], 'calls': record['model_calls'],
                          'delegations': record['delegations'], 'cny': record['accounted_and_reserved_cny']}, ensure_ascii=False), flush=True)
    groups = {}
    for arm in ARMS:
        chosen = [r for r in records if r['arm'] == arm]
        times = sorted(r['latency_seconds'] for r in chosen)
        cost = sum((Decimal(r['accounted_and_reserved_cny']) for r in chosen), Decimal(0))
        passed = sum(r['evaluation']['passed'] for r in chosen)
        groups[arm] = {'runs': len(chosen), 'passed': passed, 'violations': sum(len(r['evaluation']['constraint_violations']) for r in chosen),
            **{key: sum(r[key] for r in chosen) for key in ('model_calls', 'successful_model_calls', 'tool_calls', 'successful_tool_calls', 'input_tokens', 'output_tokens', 'delegations')},
            'tasks_with_delegation': sum(r['delegations'] > 0 for r in chosen), 'cost_cny': str(cost),
            'mean_cost_cny': str(cost / len(chosen)), 'cost_per_accepted_cny': str(cost / passed) if passed else None,
            'mean_latency_seconds': statistics.mean(times), 'median_latency_seconds': statistics.median(times),
            'p95_latency_seconds': times[(95 * len(times) + 99) // 100 - 1],
            'failures': dict(Counter(f for r in chosen for f in r['evaluation']['failures'])),
            'families': {f: {'runs': sum(r['family'] == f for r in chosen), 'passed': sum(r['family'] == f and r['evaluation']['passed'] for r in chosen)} for f in FAMILIES}}
    paired = {}
    rng = random.Random(SEED + 1)
    for arm in ('coordinator', 'on_demand'):
        differences = []
        for ident in rows:
            pair = {r['arm']: r['evaluation']['passed'] for r in records if r['case_id'] == ident}
            differences.append(int(pair[arm]) - int(pair['single']))
        samples = sorted(sum(rng.choices(differences, k=len(differences))) / len(differences) for _ in range(10000))
        paired[arm + '_vs_single'] = {'win': differences.count(1), 'tie': differences.count(0), 'loss': differences.count(-1),
            'acceptance_difference': statistics.mean(differences), 'paired_bootstrap_95_percentile': [samples[249], samples[9749]],
            'scope': 'Descriptive resampling of developer-authored case pairs; no population or real merchant inference.'}
    assert len(records) == 216
    summary = {'completed_at': datetime.now(timezone.utc).isoformat(), 'registration_sha256': sha(OUT / 'registration.json'),
        'groups': groups, 'paired': paired, 'ledger_before': registration['ledger_before'], 'ledger_after': ledger(),
        'result_sha256': {p.relative_to(OUT).as_posix(): sha(p) for p in sorted((OUT / 'runs').glob('*/result.json'))},
        'new_real_users': 0, 'deployment_changed': False, 'model_selection_changed': False}
    save(OUT / 'summary.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k != 'result_sha256'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('prepare', 'run'))
    args = parser.parse_args()
    (prepare if args.phase == 'prepare' else run)()
