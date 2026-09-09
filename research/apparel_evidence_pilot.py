"""Registered 54-run developer pilot for one citation-directory intervention."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import sys

from apparel_fulfillment.agent import ApparelAgent, ARMS, compact, pointer
from apparel_fulfillment.agent_evidence_v2 import EvidenceDirectoryAgent, citation_directory
from apparel_fulfillment.data import ROOT, digest, load_world
from apparel_fulfillment.route_audit import audit_route
from apparel_fulfillment.store import ApparelStore
from apparel_fulfillment.transport import instant, iso
from delivery_budget import operational_ledger
from research.apparel_analysis import aggregate
from research.apparel_cases import DEV_SKUS, FAMILIES, eligible_alternatives, setup
from research.apparel_evaluation import score
from research.apparel_experiment import save, sha, validate
from research.apparel_task_audit_fixed import task_audit

DIRECTORY = ROOT / 'evidence/apparel_evidence_pilot_v2'
CASE_FILE = DIRECTORY / 'cases.json'
REGISTERED_FILES = ('apparel_fulfillment/agent_evidence_v2.py', 'research/apparel_evidence_pilot.py',
                    'research/APPAREL_EVIDENCE_PILOT_PROTOCOL.md', 'research/apparel_task_audit_fixed.py')
CONDITIONS = ('original', 'directory')


def cases():
    world, rows = load_world(), []
    for index, family in enumerate(FAMILIES):
        target = sorted(DEV_SKUS)[index % len(DEV_SKUS)]
        v = world['variants'][target]
        now = datetime(2026, 11, 2 + index, 1, tzinfo=timezone.utc)
        ready = now + timedelta(days=1, hours=2)
        line = {'line_id': '1', 'requested_sku': target, 'quantity': 18 + 2 * (index % 3), 'unit': 'piece',
                'brand': v['brand'], 'style_id': v['style_id'], 'category': v['category'], 'color': v['color'], 'size': v['size']}
        request = {'sales_region': 'DE', 'wholesale': True, 'needs_shipping': family.startswith('shipping') or family == 'combined', 'lines': [line]}
        if request['needs_shipping']:
            request['shipping'] = {'destination': 'DE-DC', 'ready_at': iso(ready),
                                   'deadline_at': iso(ready + timedelta(days=7)), 'budget_cents': 25000}
        expected = {'status': 'ready', 'selected_skus': [target], 'proposal': 'new', 'must_read_variant': True}
        case = {'id': 'ECDEV-' + family, 'partition': 'evidence_directory_development', 'family': family,
                'target_sku': target, 'now': iso(now), 'request': request, 'initial_sku': target,
                'stock_overrides': {target: 90}, 'expected': expected, 'scenario_kind': None}
        if family == 'product_info':
            expected.update(status='information', proposal='none', info_field='material', unchanged=True)
            case['task'] = f'只读取 {target} 的材质原文及来源，不修改候选或订单。'
        elif family == 'order_ready':
            case['initial_sku'] = None
            case['task'] = '核对服装订单，选择与要求一致的变体，库存及规则满足后准备无运输的订单提案。引用核验依据，不确认订单。'
        elif family == 'order_clarification':
            line['unit'] = None
            expected.update(status='needs_clarification', proposal='none', issue='quantity_unit_unspecified', unchanged=True, must_read_variant=False)
            case['task'] = '检查这笔订单缺少什么已确认条件，给出具体依据，等我补充；不更改需求或生成提案。'
        elif family == 'rule_blocked':
            request['sales_region'] = 'JP'
            expected.update(status='unfulfillable', proposal='none', issue='sales_region_not_allowed', unchanged=True, must_read_variant=False)
            case['task'] = '核验当前日本销售地区是否符合服装规则，引用具体问题。保持原地区和数量，不准备违规提案。'
        elif family in ('shortage_alternative', 'combined'):
            case['stock_overrides'] = {sku: (0 if sku == target else 90) for sku in world['variants']}
            alternative_world = deepcopy(world)
            for sku, quantity in case['stock_overrides'].items():
                alternative_world['stock'][sku]['available_catalog_units'] = quantity
            eligible, differences = eligible_alternatives(request, target, alternative_world)
            expected.update(status='needs_clarification', selected_skus=eligible, proposal='none',
                            issue='substitution_requires_confirmation', minimum_differences=differences,
                            must_read_alternatives=True, must_read_variant=False)
            case['task'] = '当前候选缺货。检查库存与规则，找出对明确要求改动最少的替代并选入供我审阅，引用具体差异。改变明确要求需等我确认；先不要准备提案。'
            if family == 'combined':
                case['approved_sku'] = eligible[0]
                case['scenario_kind'] = 'cancel'
                expected.update(status='ready', selected_skus=[eligible[0]], proposal='revision',
                                must_read_alternatives=False, must_read_variant=True, event_read=True, old_valid=False)
                expected.pop('issue')
                case['task'] = '替代已获得单独批准。核验这个替代SKU和规则，读取运输事件并核验旧提案；如果失效，在原预算和交期下修订，引用新旧状态、费用和送达时间，不确认订单。'
        elif family == 'shipping_normal':
            case['task'] = '核对服装及库存规则，按已确认运输要求准备提案；给出订单状态、费用、到达时间与来源，不确认订单。'
        elif family == 'shipping_infeasible':
            request['shipping']['budget_cents'] = 1
            expected.update(status='unfulfillable', proposal='infeasible', infeasibility_certificate='Every outgoing leg costs at least 1500 cents; budget is 1 cent.')
            case['task'] = '核验订单后按原预算交期尝试运输规划。如果无解，给出无解依据和需要我选择的调整方向，不能自动放宽条件。'
        elif family == 'shipping_revision':
            case['scenario_kind'] = 'cancel'
            expected.update(proposal='revision', event_read=True, old_valid=False)
            case['task'] = '班次条件可能变化。读取事件并核验旧提案，必要时按原约束修订，引用订单状态、新旧有效性、费用及到达时间；不要确认。'
        rows.append(case)
    return rows


def clone(source, target):
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def fixture_check(case, initial, seed, world):
    path = initial / 'free_check.sqlite'
    clone(initial / 'operations.sqlite', path)
    store = ApparelStore(path, world=world)
    owner, draft_id, now = 'evaluation', seed['draft_id'], instant(seed['now'])
    if case['family'] == 'order_ready':
        view = store.view(owner, draft_id)
        store.select(owner, draft_id, [{'line_id': '1', 'sku': case['target_sku']}], expected_revision=view['revision'])
    elif case['family'] == 'shortage_alternative':
        view = store.view(owner, draft_id)
        store.select(owner, draft_id, [{'line_id': '1', 'sku': case['expected']['selected_skus'][0]}], expected_revision=view['revision'])
    view = store.view(owner, draft_id)
    if case['expected'].get('issue'):
        assert case['expected']['issue'] in {i['code'] for i in view['order_check']['issues']}
    old_invalid = None
    if case['scenario_kind']:
        old_invalid = not store.assess(owner, draft_id, view['proposals'][-1]['proposal_id'], now=now)['valid']
        assert old_invalid
    route_audit = None
    if case['expected']['proposal'] != 'none':
        p = store.propose(owner, draft_id, expected_revision=view['revision'], now=now)
        if case['expected']['proposal'] == 'infeasible':
            assert p['route']['status'] == 'infeasible'
        else:
            assert p['route']['status'] == ('planned' if case['request']['needs_shipping'] else 'not_required')
            route_audit = audit_route(view['order_check'], case['request'].get('shipping'), p['route'], world,
                                      store.corridor, events=store.transport_events(), now=now)
            assert route_audit['passed']
    return {'case_id': case['id'], 'passed': True, 'old_invalid': old_invalid, 'route_audit': route_audit,
            'notice': 'Deterministic fixture check; no agent or model success is claimed.'}


def prepare():
    validate()
    if (DIRECTORY / 'registration.json').exists():
        raise FileExistsError('Pilot is already registered; do not re-register around outcomes')
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    rows = cases()
    save(CASE_FILE, {'scope': 'Newly authored developer scenarios over the same known catalogue', 'cases': rows})
    checks, initial_files = [], {}
    for case in rows:
        folder = DIRECTORY / 'initial' / case['id']; folder.mkdir(parents=True, exist_ok=True)
        if (folder / 'operations.sqlite').exists():
            raise FileExistsError('Preserve prior partial fixture setup: ' + str(folder))
        store, draft_id, now = setup(case, folder / 'operations.sqlite')
        world = store.base_world
        seed = {'draft_id': draft_id, 'now': iso(now), 'initial_view_digest': digest(store.view('evaluation', draft_id)),
                'events_digest': digest(store.transport_events())}
        save(folder / 'world.json', world); save(folder / 'seed.json', seed)
        checks.append(fixture_check(case, folder, seed, world))
        for name in ('world.json', 'seed.json', 'operations.sqlite'):
            p = folder / name; initial_files[p.relative_to(ROOT).as_posix()] = sha(p)
    # Replay only path generation against old observations, never model execution.
    source_audit = json.loads((ROOT / 'evidence/apparel_strategy_v1/test/task_audit_v3.json').read_text(encoding='utf-8'))
    observations = paths = 0
    for name, expected in source_audit['source_results_sha256'].items():
        p = ROOT / name
        if sha(p) != expected: raise ValueError('Frozen result changed')
        run = json.loads(p.read_text(encoding='utf-8'))
        for obs in run['observations'].values():
            before = deepcopy(obs)
            catalogue = citation_directory(obs)
            for path in catalogue['paths']:
                assert len(compact(pointer(obs, path))) <= 2500
                paths += 1
            assert obs == before
            observations += 1
    save(DIRECTORY / 'free_checks.json', {'cases': checks, 'existing_observations_checked': observations,
                                        'resolvable_citation_paths': paths, 'new_model_calls': 0})
    jobs = [(c['id'], condition, arm) for c in rows for condition in CONDITIONS for arm in ARMS]
    random.Random(26090817).shuffle(jobs)
    registration = {'registered_at': datetime.now(timezone.utc).isoformat(), 'scope': 'development_only',
                    'jobs': jobs, 'expected_runs': 54, 'max_workers': 3,
                    'case_sha256': sha(CASE_FILE), 'initial_files_sha256': initial_files,
                    'base_method_sha256': sha(ROOT / 'evidence/apparel_strategy_v1/method.json'),
                    'method_files_sha256': {p: sha(ROOT / p) for p in REGISTERED_FILES},
                    'free_checks_sha256': sha(DIRECTORY / 'free_checks.json'), 'ledger_at_registration': operational_ledger().summary()}
    save(DIRECTORY / 'registration.json', registration)
    print(json.dumps({'registered_runs': 54, 'free_fixture_checks': len(checks), 'observations': observations, 'paths': paths}, ensure_ascii=False))


def validate_pilot():
    validate()
    value = json.loads((DIRECTORY / 'registration.json').read_text(encoding='utf-8'))
    assert sha(CASE_FILE) == value['case_sha256']
    assert sha(ROOT / 'evidence/apparel_strategy_v1/method.json') == value['base_method_sha256']
    assert sha(DIRECTORY / 'free_checks.json') == value['free_checks_sha256']
    for name, expected in (value['initial_files_sha256'] | value['method_files_sha256']).items():
        if sha(ROOT / name) != expected: raise ValueError('Pilot input changed: ' + name)
    return value


def execute(case, condition, arm):
    folder = DIRECTORY / 'runs' / case['id'] / condition / arm
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / 'result.json').exists(): return 'already_recorded'
    if (folder / 'attempt.json').exists(): raise RuntimeError('Incomplete attempt must not be retried: ' + str(folder))
    initial = DIRECTORY / 'initial' / case['id']
    seed = json.loads((initial / 'seed.json').read_text(encoding='utf-8'))
    world = json.loads((initial / 'world.json').read_text(encoding='utf-8'))
    clone(initial / 'operations.sqlite', folder / 'operations.sqlite')
    store = ApparelStore(folder / 'operations.sqlite', world=world)
    assert digest(store.view('evaluation', seed['draft_id'])) == seed['initial_view_digest']
    assert digest(store.transport_events()) == seed['events_digest']
    cls = ApparelAgent if condition == 'original' else EvidenceDirectoryAgent
    now = instant(seed['now'])
    agent = cls(store, 'evaluation', seed['draft_id'], arm=arm, now=now, phase='evidence_pilot_' + condition)
    with (folder / 'attempt.json').open('x', encoding='utf-8') as handle:
        json.dump({'case_id': case['id'], 'condition': condition, 'arm': arm, 'run_id': agent.run_id,
                   'purpose': agent.purpose, 'registration_sha256': sha(DIRECTORY / 'registration.json')}, handle)
    result = agent.run(case['task'])
    result.update(case_id=case['id'], family=case['family'], target_sku=case['target_sku'], condition=condition,
                  partition='development_only', initial_view_digest=seed['initial_view_digest'],
                  registration_sha256=sha(DIRECTORY / 'registration.json'))
    # Preserve raw execution even if a scoring exception occurs; never rerun it.
    save(folder / 'execution.json', result)
    result['score'] = score(case, result, store, now)
    result['task_audit'] = task_audit(case, result)
    save(folder / 'result.json', result)
    return {k: result[k] for k in ('case_id', 'condition', 'arm', 'run_status', 'accounted_and_reserved_cny')}


def run():
    registration = validate_pilot()
    by_id = {c['id']: c for c in json.loads(CASE_FILE.read_text(encoding='utf-8'))['cases']}
    with ThreadPoolExecutor(max_workers=registration['max_workers']) as pool:
        futures = [pool.submit(execute, by_id[c], condition, arm) for c, condition, arm in registration['jobs']]
        for future in as_completed(futures):
            print(json.dumps(future.result(), ensure_ascii=False), flush=True)
    validate_pilot()
    summarize()


def summarize():
    registration = validate_pilot()
    records, hashes = [], {}
    for case_id, condition, arm in registration['jobs']:
        p = DIRECTORY / 'runs' / case_id / condition / arm / 'result.json'
        if not p.exists(): raise ValueError('No comparison report before every planned run is recorded')
        records.append(json.loads(p.read_text(encoding='utf-8')))
        hashes[p.relative_to(ROOT).as_posix()] = sha(p)
    summaries = {}
    for condition in CONDITIONS:
        summaries[condition] = {}
        for arm in ARMS:
            rows = [r for r in records if r['condition'] == condition and r['arm'] == arm]
            corrected = [r | {'score': r['task_audit']} for r in rows]
            summaries[condition][arm] = {'original_strict': aggregate(rows), 'supplementary': aggregate(corrected)}
    output = {'scope': 'Completed developer pilot, not held-out performance or default selection', 'runs': len(records),
              'registration_sha256': sha(DIRECTORY / 'registration.json'), 'arms': summaries,
              'source_results_sha256': hashes, 'ledger_after': operational_ledger().summary()}
    save(DIRECTORY / 'summary.json', output)
    print(json.dumps({'status': 'pilot_complete', 'runs': len(records), 'summary': str(DIRECTORY / 'summary.json')}, ensure_ascii=False))


if __name__ == '__main__':
    {'prepare': prepare, 'run': run, 'summarize': summarize}[sys.argv[1]]()
