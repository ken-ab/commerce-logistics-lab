"""Prospectively frozen 2x2x3 development pilot; no automatic reattempts."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import random
import sys

from apparel_fulfillment.action_contract import TaskContract
from apparel_fulfillment.agent import ARMS
from apparel_fulfillment.agent_state_v3 import StateContractAgent
from apparel_fulfillment.data import ROOT, digest
from apparel_fulfillment.route_audit import audit_route
from apparel_fulfillment.store import ApparelStore
from apparel_fulfillment.transport import instant, iso
from delivery_budget import operational_ledger
from research.apparel_analysis import aggregate
from research.apparel_evidence_pilot import clone, validate_pilot as validate_directory
from research.apparel_experiment import save, sha
from research.apparel_state_cases_v3 import cases, setup_case
from research.apparel_state_evaluation_v3 import evaluate

DIRECTORY = ROOT / 'evidence/apparel_state_pilot_v3'
CONDITIONS = {'neither': (False, False), 'bootstrap': (True, False), 'guard': (False, True), 'both': (True, True)}
REGISTERED_FILES = ('apparel_fulfillment/action_contract.py', 'apparel_fulfillment/agent_state_v3.py',
    'research/apparel_state_cases_v3.py', 'research/apparel_state_evaluation_v3.py', 'research/apparel_state_pilot_v3.py',
    'research/APPAREL_STATE_PILOT_V3_PROTOCOL.md', 'docs/adr/0003-apparel-action-contract.md',
    'tests/test_apparel_state_v3.py', 'tests/test_apparel_state_pilot_v3.py',
    'evidence/apparel_state_v3_unit_tests.xml', 'evidence/apparel_state_pilot_v3_tests.xml')


def fixture_check(case, initial, seed, world):
    clone(initial / 'operations.sqlite', initial / 'free_check.sqlite')
    store = ApparelStore(initial / 'free_check.sqlite', world=world)
    owner, draft_id, now = 'evaluation', seed['draft_id'], instant(seed['now'])
    before = store.view(owner, draft_id)
    view = before
    if case['scenario'] in ('order_ready', 'multiple_lines', 'shortage_alternative'):
        choices = [{'line_id': line, 'sku': allowed[0]} for line, allowed in case['expected']['allowed_by_line'].items()]
        view = store.select(owner, draft_id, choices, expected_revision=view['revision'])
    expected = case['expected']
    assert len(view['selections']) == len(expected['allowed_by_line'])
    assert all(s['sku'] in expected['allowed_by_line'][s['line_id']] for s in view['selections'])
    if expected.get('issue'):
        assert expected['issue'] in {i['code'] for i in view['order_check']['issues']}
    expected_order_status = expected['status'] if expected['status'] in ('needs_clarification', 'unfulfillable') and expected['proposal'] == 'none' else 'ready'
    assert view['order_check']['status'] == expected_order_status
    old_valid, independent = None, None
    if case['scenario_kind']:
        old_valid = store.assess(owner, draft_id, view['proposals'][-1]['proposal_id'], now=now)['valid']
        assert old_valid == expected['old_valid']
    if expected['proposal'] != 'none':
        proposal = view['proposals'][-1] if expected['proposal'] == 'keep' else store.propose(owner, draft_id, expected_revision=view['revision'], now=now)
        if expected['proposal'] == 'infeasible':
            assert proposal['route']['status'] == 'infeasible'
            assert proposal['route']['adjustment_options']
            assert all(c['requires_user_choice'] for c in proposal['route']['adjustment_options'])
        else:
            assert proposal['route']['status'] == ('planned' if case['request']['needs_shipping'] else 'not_required')
            independent = audit_route(view['order_check'], case['request'].get('shipping'), proposal['route'], world,
                                      store.corridor, events=store.transport_events(), now=now)
            assert independent['passed']
    after = store.view(owner, draft_id)
    assert after['request'] == before['request'] and after['approved_substitutions'] == before['approved_substitutions']
    assert after['confirmation'] == before['confirmation'] is None
    return {'case_id': case['id'], 'passed': True, 'old_valid': old_valid, 'route_audit': independent,
            'notice': 'Fixture feasibility only; zero model calls.'}


def prepare():
    validate_directory()
    if DIRECTORY.exists(): raise FileExistsError('Preserve any earlier setup and its failures; do not overwrite it')
    DIRECTORY.mkdir(parents=True)
    rows, initial_files, checks = cases(), {}, []
    save(DIRECTORY / 'cases.json', {'scope': 'Development only; known catalogue and task families', 'cases': rows})
    for case in rows:
        TaskContract.model_validate(case['contract'])
        initial = DIRECTORY / 'initial' / case['id']; initial.mkdir(parents=True)
        store, draft_id, now = setup_case(case, initial / 'operations.sqlite')
        seed = {'draft_id': draft_id, 'now': iso(now), 'initial_view_digest': digest(store.view('evaluation', draft_id)),
                'events_digest': digest(store.transport_events())}
        save(initial / 'world.json', store.base_world); save(initial / 'seed.json', seed)
        checks.append(fixture_check(case, initial, seed, store.base_world))
        for name in ('world.json', 'seed.json', 'operations.sqlite'):
            path = initial / name; initial_files[path.relative_to(ROOT).as_posix()] = sha(path)
    save(DIRECTORY / 'free_checks.json', {'cases': checks, 'new_model_calls': 0})
    jobs = [(c['id'], condition, arm) for c in rows for condition in CONDITIONS for arm in ARMS]
    random.Random(26090831).shuffle(jobs)
    registration = {'registered_at': datetime.now(timezone.utc).isoformat(), 'scope': 'development_only',
        'jobs': jobs, 'conditions': CONDITIONS, 'expected_runs': len(jobs), 'max_workers': 3,
        'case_sha256': sha(DIRECTORY / 'cases.json'), 'initial_files_sha256': initial_files,
        'directory_registration_sha256': sha(ROOT / 'evidence/apparel_evidence_pilot_v2/registration.json'),
        'method_files_sha256': {name: sha(ROOT / name) for name in REGISTERED_FILES},
        'free_checks_sha256': sha(DIRECTORY / 'free_checks.json'), 'ledger_at_registration': operational_ledger().summary()}
    save(DIRECTORY / 'registration.json', registration)
    print(json.dumps({'registered_runs': len(jobs), 'free_fixture_checks': len(checks),
                      'registration_sha256': sha(DIRECTORY / 'registration.json')}, ensure_ascii=False))


def validate_pilot():
    validate_directory()
    value = json.loads((DIRECTORY / 'registration.json').read_text(encoding='utf-8'))
    assert sha(ROOT / 'evidence/apparel_evidence_pilot_v2/registration.json') == value['directory_registration_sha256']
    assert sha(DIRECTORY / 'cases.json') == value['case_sha256']
    assert sha(DIRECTORY / 'free_checks.json') == value['free_checks_sha256']
    for name, expected in (value['initial_files_sha256'] | value['method_files_sha256']).items():
        if sha(ROOT / name) != expected: raise ValueError('Frozen state pilot input changed: ' + name)
    return value


def execute(case, condition, arm):
    folder = DIRECTORY / 'runs' / case['id'] / condition / arm
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / 'result.json').exists(): return 'already_recorded'
    if (folder / 'attempt.json').exists(): raise RuntimeError('Do not automatically reattempt: ' + str(folder))
    initial = DIRECTORY / 'initial' / case['id']
    seed = json.loads((initial / 'seed.json').read_text(encoding='utf-8'))
    world = json.loads((initial / 'world.json').read_text(encoding='utf-8'))
    clone(initial / 'operations.sqlite', folder / 'operations.sqlite')
    store, now = ApparelStore(folder / 'operations.sqlite', world=world), instant(seed['now'])
    assert digest(store.view('evaluation', seed['draft_id'])) == seed['initial_view_digest']
    assert digest(store.transport_events()) == seed['events_digest']
    bootstrap, guard = CONDITIONS[condition]
    agent = StateContractAgent(store, 'evaluation', seed['draft_id'], arm=arm, now=now, contract=case['contract'],
                               bootstrap=bootstrap, enforce_contract=guard, phase='state_v3_' + condition)
    with (folder / 'attempt.json').open('x', encoding='utf-8') as handle:
        json.dump({'case_id': case['id'], 'condition': condition, 'arm': arm, 'run_id': agent.run_id,
                   'purpose': agent.purpose, 'registration_sha256': sha(DIRECTORY / 'registration.json')}, handle)
    result = agent.run(case['task'])
    result.update(case_id=case['id'], scenario=case['scenario'], family=case['family'], target_sku=case['target_sku'],
                  condition=condition, partition='development_only', initial_view_digest=seed['initial_view_digest'],
                  registration_sha256=sha(DIRECTORY / 'registration.json'))
    save(folder / 'execution.json', result)
    result.update(evaluate(case, result, store, now))
    save(folder / 'result.json', result)
    return {k: result[k] for k in ('case_id', 'condition', 'arm', 'run_status', 'accounted_and_reserved_cny')}


def run():
    registration = validate_pilot()
    by_id = {c['id']: c for c in json.loads((DIRECTORY / 'cases.json').read_text(encoding='utf-8'))['cases']}
    # Check all previous attempts before scheduling, so a partial failure cannot
    # silently relaunch this condition while other jobs are already starting.
    for case_id, condition, arm in registration['jobs']:
        folder = DIRECTORY / 'runs' / case_id / condition / arm
        if (folder / 'attempt.json').exists() and not (folder / 'result.json').exists():
            raise RuntimeError('An incomplete prior attempt must be accounted for explicitly: ' + str(folder))
    with ThreadPoolExecutor(max_workers=registration['max_workers']) as pool:
        futures = [pool.submit(execute, by_id[c], condition, arm) for c, condition, arm in registration['jobs']]
        for future in as_completed(futures):
            print(json.dumps(future.result(), ensure_ascii=False), flush=True)
    summarize()


def summarize():
    registration = validate_pilot()
    records, hashes = [], {}
    for case_id, condition, arm in registration['jobs']:
        path = DIRECTORY / 'runs' / case_id / condition / arm / 'result.json'
        if not path.exists(): raise ValueError('All planned results are required before final reporting')
        records.append(json.loads(path.read_text(encoding='utf-8')))
        hashes[path.relative_to(ROOT).as_posix()] = sha(path)
    groups = {condition: {arm: aggregate([r for r in records if r['condition'] == condition and r['arm'] == arm])
                          for arm in ARMS} for condition in CONDITIONS}
    save(DIRECTORY / 'summary.json', {'scope': 'Completed development factorial pilot; no default change',
        'runs': len(records), 'registration_sha256': sha(DIRECTORY / 'registration.json'), 'groups': groups,
        'source_results_sha256': hashes, 'ledger_after': operational_ledger().summary()})
    print(json.dumps({'status': 'complete', 'runs': len(records), 'summary': str(DIRECTORY / 'summary.json')}, ensure_ascii=False))


if __name__ == '__main__':
    {'prepare': prepare, 'run': run, 'summarize': summarize}[sys.argv[1]]()
