"""Separately registered replication; frozen v3 runtime, new order states."""
from datetime import datetime, timezone
import json
import os
import random
import sys

from apparel_fulfillment.agent import ARMS, MODEL
from apparel_fulfillment.agent_state_v3 import StateContractAgent
from apparel_fulfillment.data import ROOT, digest
from apparel_fulfillment.store import ApparelStore
from apparel_fulfillment.transport import instant, iso
from delivery_budget import operational_ledger
from research.apparel_analysis import aggregate
from research.apparel_evidence_pilot import clone
from research.apparel_experiment import save, sha
from research.apparel_state_cases_v3 import setup_case
from research.apparel_state_evaluation_v3 import evaluate
from research.apparel_state_pilot_v3 import CONDITIONS, fixture_check, validate_pilot
from research.apparel_state_replication_cases import cases
from research.guarded_dispatch import dispatch
from research.provider_gate import ProviderGate, ProviderHeld, guarded_business_client

DIRECTORY = ROOT / 'evidence/apparel_state_replication_v3'
FILES = ('research/apparel_state_replication_v3.py', 'research/apparel_state_replication_cases.py',
         'research/APPAREL_STATE_REPLICATION_PROTOCOL.md', 'research/provider_gate.py', 'research/guarded_dispatch.py',
         'tests/test_apparel_state_replication.py', 'tests/test_provider_gate.py',
         'evidence/apparel_state_replication_fixture_tests.xml', 'evidence/provider_gate_and_report_tests_20260908.xml')


def prepare():
    validate_pilot()
    if DIRECTORY.exists(): raise FileExistsError('Preserve existing replication setup')
    DIRECTORY.mkdir(parents=True)
    rows, initial_files, checks = cases(), {}, []
    save(DIRECTORY / 'cases.json', {'scope': 'new_order_states_known_catalogue', 'cases': rows})
    for case in rows:
        initial = DIRECTORY / 'initial' / case['id']; initial.mkdir(parents=True)
        store, draft_id, now = setup_case(case, initial / 'operations.sqlite')
        seed = {'draft_id': draft_id, 'now': iso(now), 'initial_view_digest': digest(store.view('evaluation', draft_id)),
                'events_digest': digest(store.transport_events())}
        save(initial / 'world.json', store.base_world); save(initial / 'seed.json', seed)
        checks.append(fixture_check(case, initial, seed, store.base_world))
        for name in ('world.json', 'seed.json', 'operations.sqlite'):
            path = initial / name; initial_files[path.relative_to(ROOT).as_posix()] = sha(path)
    save(DIRECTORY / 'free_checks.json', {'cases': checks, 'new_model_calls': 0})
    rng = random.Random(26090841)
    order = [c['id'] for c in rows]; rng.shuffle(order)
    blocks = []
    for ident in order:
        jobs = [(ident, condition, arm) for condition in CONDITIONS for arm in ARMS]; rng.shuffle(jobs)
        blocks.append(jobs)
    registration = {'registered_at': datetime.now(timezone.utc).isoformat(), 'scope': 'new_order_states_known_catalogue',
        'blocks': blocks, 'jobs': [j for block in blocks for j in block], 'expected_runs': 144, 'max_workers': 3,
        'initial_files_sha256': initial_files, 'case_sha256': sha(DIRECTORY / 'cases.json'),
        'free_checks_sha256': sha(DIRECTORY / 'free_checks.json'),
        'pilot_registration_sha256': sha(ROOT / 'evidence/apparel_state_pilot_v3/registration.json'),
        'method_files_sha256': {p: sha(ROOT / p) for p in FILES},
        'recovery_evidence_sha256': sha(ROOT / 'evidence/aihubmix_recovery_probe_20260908.json'),
        'ledger_at_registration': operational_ledger().summary()}
    save(DIRECTORY / 'registration.json', registration)
    print(json.dumps({'registered_runs': 144, 'free_checks': len(checks), 'registration_sha256': sha(DIRECTORY / 'registration.json')}))


def validate():
    validate_pilot()
    r = json.loads((DIRECTORY / 'registration.json').read_text(encoding='utf-8'))
    assert sha(ROOT / 'evidence/apparel_state_pilot_v3/registration.json') == r['pilot_registration_sha256']
    assert sha(DIRECTORY / 'cases.json') == r['case_sha256']
    assert sha(DIRECTORY / 'free_checks.json') == r['free_checks_sha256']
    assert sha(ROOT / 'evidence/aihubmix_recovery_probe_20260908.json') == r['recovery_evidence_sha256']
    for name, expected in (r['initial_files_sha256'] | r['method_files_sha256']).items():
        if sha(ROOT / name) != expected: raise ValueError('Frozen replication input changed: ' + name)
    return r


def execute(case, condition, arm, gate):
    client = guarded_business_client(gate)
    try:
        client.ensure_available(MODEL)
    except ProviderHeld:
        return {'not_started': True, 'reason': 'provider_hold'}
    folder = DIRECTORY / 'runs' / case['id'] / condition / arm
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / 'result.json').exists(): return {'already_recorded': True}
    if (folder / 'attempt.json').exists(): raise RuntimeError('No automatic reattempt: ' + str(folder))
    initial = DIRECTORY / 'initial' / case['id']
    seed = json.loads((initial / 'seed.json').read_text(encoding='utf-8'))
    world = json.loads((initial / 'world.json').read_text(encoding='utf-8'))
    clone(initial / 'operations.sqlite', folder / 'operations.sqlite')
    store, now = ApparelStore(folder / 'operations.sqlite', world=world), instant(seed['now'])
    assert digest(store.view('evaluation', seed['draft_id'])) == seed['initial_view_digest']
    assert digest(store.transport_events()) == seed['events_digest']
    bootstrap, guard = CONDITIONS[condition]
    agent = StateContractAgent(store, 'evaluation', seed['draft_id'], arm=arm, now=now, client=client,
             contract=case['contract'], bootstrap=bootstrap, enforce_contract=guard, phase='state_replication_' + condition)
    with (folder / 'attempt.json').open('x', encoding='utf-8') as h:
        json.dump({'case_id': case['id'], 'condition': condition, 'arm': arm, 'run_id': agent.run_id,
                   'purpose': agent.purpose, 'registration_sha256': sha(DIRECTORY / 'registration.json')}, h)
    result = agent.run(case['task'])
    result.update(case_id=case['id'], scenario=case['scenario'], family=case['family'], target_sku=case['target_sku'],
                  condition=condition, partition='new_order_states_known_catalogue', initial_view_digest=seed['initial_view_digest'],
                  registration_sha256=sha(DIRECTORY / 'registration.json'))
    save(folder / 'execution.json', result)
    result.update(evaluate(case, result, store, now))
    save(folder / 'result.json', result)
    return {k: result[k] for k in ('case_id', 'condition', 'arm', 'run_status', 'accounted_and_reserved_cny')}


def summarize():
    registration = validate()
    paths = sorted((DIRECTORY / 'runs').glob('*/*/*/result.json'))
    if len(paths) != registration['expected_runs']: raise ValueError('Only a complete replication can be summarized')
    rows = [json.loads(p.read_text(encoding='utf-8')) for p in paths]
    assert {(r['case_id'], r['condition'], r['arm']) for r in rows} == {tuple(j) for j in registration['jobs']}
    save(DIRECTORY / 'summary.json', {'runs': len(rows), 'registration_sha256': sha(DIRECTORY / 'registration.json'),
         'groups': {c: {a: aggregate([r for r in rows if r['condition']==c and r['arm']==a]) for a in ARMS} for c in CONDITIONS},
         'source_results_sha256': {p.relative_to(ROOT).as_posix(): sha(p) for p in paths},
         'ledger_after': operational_ledger().summary()})


def run():
    registration = validate()
    by_id = {c['id']: c for c in json.loads((DIRECTORY / 'cases.json').read_text(encoding='utf-8'))['cases']}
    for ident, condition, arm in registration['jobs']:
        p = DIRECTORY / 'runs' / ident / condition / arm
        if (p / 'attempt.json').exists() and not (p / 'result.json').exists():
            raise RuntimeError('Account for incomplete prior attempt explicitly: ' + str(p))
    gate = ProviderGate(ROOT / 'evidence/provider_availability.sqlite')
    gate.check('aihubmix')
    lock = DIRECTORY / 'launch.json'
    with lock.open('x', encoding='utf-8') as h:
        json.dump({'pid': os.getpid(), 'at': datetime.now(timezone.utc).isoformat(), 'status': 'running'}, h)
    try:
        for block in registration['blocks']:
            for job, result in dispatch(block, lambda j: execute(by_id[j[0]], j[1], j[2], gate),
                                        lambda: gate.check('aihubmix'), max_workers=registration['max_workers']):
                print(json.dumps(result, ensure_ascii=False), flush=True)
        summarize()
    except Exception as error:
        save(DIRECTORY / 'driver_status.json', {'status': 'stopped', 'error_type': type(error).__name__, 'no_automatic_retry': True})
        raise
    save(DIRECTORY / 'driver_status.json', {'status': 'complete', 'runs': 144, 'at': datetime.now(timezone.utc).isoformat()})
    print(json.dumps({'complete': 144}), flush=True)


if __name__ == '__main__':
    {'prepare': prepare, 'validate': validate, 'run': run, 'summarize': summarize}[sys.argv[1]]()
