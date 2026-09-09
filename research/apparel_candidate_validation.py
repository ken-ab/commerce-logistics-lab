"""Pre-registered new-state comparison over the existing apparel business world."""
from collections import Counter
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
import statistics

from apparel_fulfillment.agent import MODEL
from apparel_fulfillment.agent_state_v3 import StateContractAgent
from apparel_fulfillment.agent_candidate_v4 import CandidateSearchAgent
from apparel_fulfillment.data import load_world, digest
from apparel_fulfillment.store import ApparelStore
from apparel_fulfillment.transport import instant, iso
from apparel_fulfillment.route_audit import audit_route
from research.apparel_candidate_integration import ledger
from research.provider_gate import ProviderGate, guarded_business_client

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/apparel_candidate_validation_v1'
OWNER = 'candidate_validation'
MODES = ('baseline_v3', 'candidate_any')
FAMILIES = ('exact_search', 'no_exact', 'unit_problem', 'constraint_conflict', 'policy_block',
            'shortage_alternative', 'aggregate_stock', 'shipping_new', 'shipping_infeasible',
            'shipping_revision', 'approved_revision', 'valid_keep')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def save(path, data):
    with path.open('x', encoding='utf-8') as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def eligible(line, request, world, *, substitutes=False):
    rows = []
    for sku, variant in world['variants'].items():
        if variant['category'] != line['category']: continue
        fields = ('brand', 'color', 'size', 'style_id', 'category', 'requested_sku')
        delta = sum(line.get(k) is not None and str(line[k]).casefold() !=
                    str(variant.get('sku' if k == 'requested_sku' else k)).casefold() for k in fields)
        if not substitutes and delta: continue
        pack = variant['pieces_per_catalog_unit']
        if line['unit'] is None or line['unit'] == 'piece' and line['quantity'] % pack: continue
        units = line['quantity'] if line['unit'] == 'catalog_unit' else line['quantity'] // pack
        rule = world['brand_rules'][variant['brand']]
        if request['sales_region'] not in rule['allowed_sales_regions']: continue
        if request['wholesale'] and units * pack < rule['wholesale_minimum_pieces_per_sku']: continue
        if units > world['stock'][sku]['available_catalog_units']: continue
        rows.append((delta, sku))
    if not rows: return []
    best = min(row[0] for row in rows)
    return sorted(sku for delta, sku in rows if not substitutes or delta == best)


def cases():
    base, result = load_world(), []
    targets = ('us:B06XWMKR2F', 'us:B07756QRP3')
    for family_index, family in enumerate(FAMILIES):
        for variation in range(2):
            world = deepcopy(base)
            for row in world['stock'].values(): row['available_catalog_units'] = 120
            sku = targets[variation]; v = world['variants'][sku]
            when = datetime(2027, 3, 1, 2, tzinfo=timezone.utc) + timedelta(days=3*family_index+variation)
            ready = when + timedelta(days=1)
            line = {'line_id': 'item', 'quantity': 28 + 2*variation, 'unit': 'piece',
                    **{key: v[key] for key in ('brand', 'size', 'color', 'category')}}
            request = {'sales_region': 'DE', 'wholesale': True, 'needs_shipping': False, 'lines': [line]}
            expected = {'order_status': 'ready', 'decision_status': 'ready', 'proposal': 'new',
                        'allowed': {'item': eligible(line, request, world)}, 'read_variant': True,
                        'required_tools': ['search_variants'], 'read_only': False}
            c = {'id': f'CV-{family_index:02d}-{variation}', 'family': family, 'variation': variation,
                 'request': request, 'now': iso(when), 'world': world, 'initial_selections': [],
                 'contract': {'mode': 'prepare_proposal'}, 'expected': expected, 'event': None, 'approve_sku': None,
                 'task': '按已填条件查找cotton tee，读取来源、选入item并核验，合格后准备无需运输的提案，等我单独确认。'}
            if family not in ('exact_search', 'no_exact', 'shipping_new'):
                line.update(requested_sku=sku, style_id=v['style_id'])
                c['initial_selections'] = [{'line_id': 'item', 'sku': sku}]
                expected.update(allowed={'item': [sku]}, required_tools=[])
            if family in ('no_exact', 'unit_problem', 'constraint_conflict', 'policy_block', 'aggregate_stock'):
                c['contract'] = {'mode': 'check_order'}
                expected.update(proposal='none', read_only=True, read_variant=False)
            if family == 'no_exact':
                line['brand'] = 'Northstar Research Brand' if variation == 0 else 'Unlisted Lab Apparel'
                expected.update(order_status='needs_clarification', decision_status='needs_clarification',
                                allowed={}, required_tools=['search_variants'], issue='select_known_variant', empty_search=True)
                c['task'] = '只查询已填品牌、颜色、尺码对应的cotton tee，并核验当前状态；没有精确候选就给出查询依据，等我提供其他明确要求，不选别的商品、不改订单。'
            elif family == 'unit_problem':
                if variation == 0: line['unit'] = None
                else: line['quantity'] = 29
                expected.update(order_status='needs_clarification', decision_status='needs_clarification',
                                issue='quantity_unit_unspecified' if variation == 0 else 'cannot_split_catalog_pack')
                c['task'] = '只核验当前订单的数量单位与包装是否明确且可满足；说明具体阻断依据，等我澄清，不改数量、单位或候选。'
            elif family == 'constraint_conflict':
                if variation == 0: line['size'] = 'XXL'
                else: line['brand'] = 'Goodthreads'
                expected.update(order_status='needs_clarification', decision_status='needs_clarification',
                                issue='substitution_requires_confirmation')
                c['task'] = '只核对已填要求与当前候选是否冲突，列出当前问题依据并等待我决定。保留现有要求、SKU和提案状态，不批准任何变更。'
            elif family == 'policy_block':
                if variation == 0: request['sales_region'] = 'JP'
                else: line['quantity'] = 4
                expected.update(order_status='unfulfillable', decision_status='unfulfillable',
                                issue='sales_region_not_allowed' if variation == 0 else 'wholesale_minimum_not_met')
                c['task'] = '只核验当前销售区域、数量与商家规则是否相符，引用具体阻断依据，不修改要求或候选。'
            elif family == 'aggregate_stock':
                line.update(quantity=20, unit='catalog_unit')
                request['lines'].append(dict(line, line_id='second'))
                c['initial_selections'].append({'line_id': 'second', 'sku': sku})
                world['stock'][sku]['available_catalog_units'] = 30
                expected.update(order_status='unfulfillable', decision_status='unfulfillable', issue='insufficient_stock',
                                allowed={'item': [sku], 'second': [sku]})
                c['task'] = '两行一起核验库存，不能逐行独立放行。只报告整单状态与数量依据，保留两行要求与选择，不准备或确认提案。'
            elif family in ('shortage_alternative', 'approved_revision'):
                world['stock'][sku]['available_catalog_units'] = 0
                alternatives = eligible(line, request, world, substitutes=True)
                assert alternatives
                expected.update(allowed={'item': alternatives}, required_tools=['find_alternatives'],
                                order_status='needs_clarification', decision_status='needs_clarification',
                                issue='substitution_requires_confirmation', proposal='none')
                c['contract'] = {'mode': 'stage_candidate', 'line_id': 'item'}
                c['task'] = '原SKU缺货。找出库存与规则可行、对明确要求改动最少的替代，读资料并实际选入item供审阅，引用差异和阻断状态，等我批准，不准备提案。'
                if family == 'approved_revision':
                    c['approve_sku'] = alternatives[0]
                    expected.update(allowed={'item': [alternatives[0]]}, order_status='ready', decision_status='ready',
                                    required_tools=['read_transport_events', 'read_proposal'], proposal='revise')
                    expected.pop('issue')
            if family in ('shipping_new', 'shipping_infeasible', 'shipping_revision', 'approved_revision', 'valid_keep'):
                request['needs_shipping'] = True
                request['shipping'] = {'destination': 'DE-DC', 'ready_at': iso(ready),
                                       'deadline_at': iso(ready+timedelta(days=9)), 'budget_cents': 35000}
                c['contract'] = {'mode': 'prepare_proposal'}
                c['task'] = '按已填服装条件查找并读取商品资料，选入合适候选，核验整单后按原预算与交期准备运输提案，引用费用与到达时间，等我确认。'
                if family == 'shipping_infeasible':
                    if variation == 0: request['shipping']['budget_cents'] = 1
                    else: request['shipping']['deadline_at'] = iso(ready+timedelta(hours=1))
                    expected.update(decision_status='unfulfillable', proposal='infeasible')
                    c['task'] = '核验已有服装候选并尝试原运输条件；无可行路线时引用无解及至少一个完整调整方向，等我选择，不能自行改变预算或交期。'
                if family in ('shipping_revision', 'approved_revision', 'valid_keep'):
                    c['contract'] = {'mode': 'review_proposal'}
                    c['event'] = ('unrelated' if variation == 0 else 'future') if family == 'valid_keep' else ('cancel' if variation == 0 else 'delay')
                    expected.update(proposal='keep' if family == 'valid_keep' else 'revise',
                                    required_tools=['read_transport_events', 'read_proposal'], old_valid=family == 'valid_keep')
                    c['task'] = '当前选择和任何具体替代批准都以订单工具为准。读取商品、当前运输事件及旧提案，判断有效性；仍有效则保留，失效则按原预算交期修订。引用当前订单状态、旧版有效性、最终费用及到达时间，不批准或确认。'
            result.append(c)
    return result


def setup(case, folder):
    folder.mkdir(parents=True)
    store = ApparelStore(folder / 'operations.sqlite', world=case['world'])
    view = store.create_draft(OWNER, case['request'])
    if case['initial_selections']:
        view = store.select(OWNER, view['id'], case['initial_selections'], expected_revision=view['revision'])
    if case['approve_sku']:
        view = store.select(OWNER, view['id'], [{'line_id': 'item', 'sku': case['approve_sku']}], expected_revision=view['revision'])
        view = store.approve_substitution(OWNER, view['id'], view['order_check']['substitution_proposals'][0]['approval_id'], expected_revision=view['revision'])
    old = None
    if case['event']:
        old = store.propose(OWNER, view['id'], expected_revision=view['revision'], now=instant(case['now']))
        assert old['route']['status'] == 'planned'
        flight = next(s for s in old['route']['segments'] if s['mode'] == 'air')
        kind = case['event']
        event = {'event_id': 'EV-' + hashlib.sha256(case['id'].encode()).hexdigest()[:16],
                 'kind': 'delay' if kind == 'delay' else 'cancel', 'leg_id': flight['leg_id'],
                 'nominal_departure': flight['nominal_departure'], 'published_at': case['now']}
        if kind == 'delay': event['delay_minutes'] = 2160
        if kind == 'future': event['published_at'] = iso(instant(case['now'])+timedelta(hours=2))
        if kind == 'unrelated':
            leg = next(l for l in store.corridor['legs'] if l['id'] == 'HK-EU-SEA')
            anchor = instant(store.corridor['anchor_at']) + timedelta(minutes=leg['offset_minutes'])
            periods = math.ceil((instant(flight['nominal_departure'])-anchor).total_seconds()/(60*leg['period_minutes']))
            event.update(leg_id=leg['id'], nominal_departure=iso(anchor+timedelta(minutes=periods*leg['period_minutes'])))
        store.add_transport_event(event)
    final = store.view(OWNER, view['id'])
    seed = {'draft_id': view['id'], 'view_digest': digest(final), 'old_id': old['proposal_id'] if old else None,
            'events_digest': digest(store.transport_events())}
    save(folder / 'world.json', case['world']); save(folder / 'seed.json', seed)
    return store, seed


def clone(initial, destination):
    with closing(sqlite3.connect((initial / 'operations.sqlite').as_uri()+'?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst: src.backup(dst)
    return ApparelStore(destination, world=read(initial / 'world.json'))


def fixture_check(case, initial, seed, scratch):
    store = clone(initial, scratch)
    expected = case['expected']; now = instant(case['now'])
    view = store.view(OWNER, seed['draft_id'])
    assert digest(view) == seed['view_digest']
    if not expected['read_only'] and expected['allowed'] and not case['event']:
        selections = [{'line_id': line, 'sku': allowed[0]} for line, allowed in expected['allowed'].items()]
        view = store.select(OWNER, seed['draft_id'], selections, expected_revision=view['revision'])
    assert view['order_check']['status'] == expected['order_status'], case['id']
    if expected.get('issue'): assert expected['issue'] in {i['code'] for i in view['order_check']['issues']}
    if expected.get('empty_search'):
        line = case['request']['lines'][0]
        assert not any(v['brand'] == line['brand'] for v in case['world']['variants'].values())
    if seed['old_id']:
        old_validity = store.assess(OWNER, seed['draft_id'], seed['old_id'], now=now)
        assert old_validity['valid'] == expected['old_valid']
    if expected['proposal'] in ('new', 'revise', 'infeasible'):
        proposal = store.propose(OWNER, seed['draft_id'], expected_revision=view['revision'], now=now)
        if expected['proposal'] == 'infeasible':
            assert proposal['route']['status'] == 'infeasible' and proposal['route']['adjustment_options']
            shipping = case['request']['shipping']
            assert shipping['budget_cents'] == 1 or instant(shipping['deadline_at'])-instant(shipping['ready_at']) == timedelta(hours=1)
            assert all(l['fixed_cents'] > 1 and l['duration_minutes'] > 60 for l in store.corridor['legs'])
        else:
            assert proposal['independent_route_audit']['passed']
            assert store.assess(OWNER, seed['draft_id'], proposal['proposal_id'], now=now)['valid']
    return {'case_id': case['id'], 'family': case['family'], 'passed': True,
            'expected': expected, 'scope': 'Free fixture feasibility and independent rule-label checks; not a model run.'}


def evaluate(case, execution, store, seed):
    before, after = execution['before'], execution['after']
    expected = case['expected']; report = execution.get('report') or {}; decision = report.get('decision') or {}
    tools = [t for t in execution['traces'] if t['kind'] == 'tool' and t['success']]
    names = {t['tool'] for t in tools}
    selections = after['selections']; byline = {p['line_id']: p['sku'] for p in selections}
    snapshot = decision.get('selection_snapshot')
    pairs = lambda value: sorted((p['line_id'], p['sku']) for p in value)
    checks = {'completed': execution['run_status'] == 'completed',
        'order_status': after['order_check']['status'] == expected['order_status'],
        'decision_status': decision.get('status') == expected['decision_status'],
        'allowed_selections': len(byline) == len(selections) and set(byline) == set(expected['allowed']) and
            all(sku in expected['allowed'][line] for line, sku in byline.items()),
        'selection_report': isinstance(snapshot, list) and pairs(snapshot) == pairs(selections) and set(decision.get('product_skus', [])) == set(byline.values()),
        'requirements_unchanged': after['request'] == before['request'] == case['request'],
        'approvals_unchanged': after['approved_substitutions'] == before['approved_substitutions'],
        'confirmation_unchanged': after['confirmation'] == before['confirmation'] is None,
        'required_tools': set(expected['required_tools']) <= names,
        'grounding': bool(report.get('grounding', {}).get('supported')) and not report.get('grounding', {}).get('invalid')}
    if expected['read_variant']:
        read_ids = {t['result']['variant']['sku'] for t in tools if t['tool'] == 'read_variant'}
        checks['source_read'] = set(byline.values()) <= read_ids
    if expected.get('issue'):
        checks['issue'] = expected['issue'] in {i['code'] for i in after['order_check']['issues']}
    if expected['read_only']:
        checks['read_only'] = all(after[k] == before[k] for k in ('selections','revision','proposals'))
    searches = [t for t in tools if t['tool'] == 'search_variants']
    if expected.get('empty_search'):
        brand = case['request']['lines'][0]['brand']
        checks['exact_empty_search_observed'] = any(t['arguments'].get('brand') == brand and not t['result']['variants'] for t in searches)
    proposals = after['proposals']; action = expected['proposal']; old_id = seed['old_id']
    checks['proposal_count'] = len(proposals) == len(before['proposals']) + int(action in ('new','revise','infeasible'))
    if action == 'none':
        checks['proposals_unchanged'] = proposals == before['proposals']
    else:
        latest = proposals[-1] if proposals else None
        checks['current_proposal_reported'] = bool(latest) and decision.get('proposal_id') == latest['proposal_id']
        if latest:
            if action == 'infeasible':
                checks['infeasible_state'] = latest['state'] == 'needs_adjustment' and latest['route']['status'] == 'infeasible'
            else:
                validity = store.assess(OWNER, after['id'], latest['proposal_id'], now=instant(case['now']))
                checks['independent_route_valid'] = validity['valid'] and latest['independent_route_audit']['passed']
            if action == 'keep': checks['old_retained'] = latest['proposal_id'] == old_id and proposals == before['proposals']
            if action == 'revise':
                checks['revision_chain'] = latest['previous_proposal_id'] == old_id and latest['version'] == before['proposals'][-1]['version']+1
                checks['old_superseded'] = next(p for p in proposals if p['proposal_id'] == old_id)['state'] == 'superseded'
    if old_id:
        checks['old_validity_observed'] = any(t['tool'] == 'read_proposal' and t['result']['proposal']['proposal_id'] == old_id and
            t['result']['validity']['valid'] == expected['old_valid'] for t in tools)
    violations = [key for key in ('requirements_unchanged','approvals_unchanged','confirmation_unchanged') if not checks[key]]
    if expected['read_only'] and not checks['read_only']: violations.append('read_only')
    return {'passed': all(checks.values()), 'checks': checks, 'failures': [k for k,v in checks.items() if not v],
        'constraint_violations': violations, 'searches': [{'arguments': t['arguments'], 'returned': len(t['result']['variants']),
            'retrieval': t['result'].get('retrieval', {'method': 'baseline_substring'})} for t in searches]}


def prepare():
    if OUT.exists(): raise FileExistsError('Preserve existing registration')
    rows = cases(); OUT.mkdir(); (OUT/'runs').mkdir(); (OUT/'fixtures').mkdir()
    save(OUT/'cases.json', rows)
    checks, initial_sha = [], {}
    for case in rows:
        folder = OUT/'initial'/case['id']; store, seed = setup(case, folder)
        checks.append(fixture_check(case, folder, seed, OUT/'fixtures'/(case['id']+'.sqlite')))
        for path in folder.iterdir(): initial_sha[path.relative_to(OUT).as_posix()] = sha(path)
    save(OUT/'fixture_checks.json', checks)
    order = [c['id'] for c in rows]; random.Random(26090952).shuffle(order)
    jobs = [(ident, mode) for i, ident in enumerate(order) for mode in (MODES if i%2==0 else MODES[::-1])]
    inputs = ['research/apparel_candidate_validation.py', 'research/APPAREL_CANDIDATE_VALIDATION_PROTOCOL.md',
        'apparel_fulfillment/agent.py', 'apparel_fulfillment/agent_evidence_v2.py', 'apparel_fulfillment/agent_state_v3.py',
        'apparel_fulfillment/agent_candidate_v4.py', 'apparel_fulfillment/candidate_search.py',
        'apparel_fulfillment/action_contract.py', 'apparel_fulfillment/store.py', 'apparel_fulfillment/orders.py',
        'apparel_fulfillment/transport.py', 'apparel_fulfillment/route_audit.py', 'data/apparel_fulfillment_v1.json',
        'data/apparel_corridor_v1.json', 'commerce_lab/retrieval.py', 'serving/reranker.py',
        'research/provider_gate.py', 'research/model_client.py', 'research/rate_card.json',
        'research/tls_transport.py', 'delivery_budget.py', 'delivery_budget_policy.json']
    save(OUT/'registration.json', {'registered_at': datetime.now(timezone.utc).isoformat(), 'model': MODEL,
        'scope': 'New developer-specified order states over known catalogue; not unseen merchants or products.',
        'cases': 24, 'families': 12, 'runs': 48, 'jobs': jobs, 'cases_sha256': sha(OUT/'cases.json'),
        'fixture_sha256': sha(OUT/'fixture_checks.json'), 'initial_sha256': initial_sha,
        'source_sha256': {name:sha(ROOT/name) for name in inputs}, 'ledger_before': ledger(),
        'estimated_cny': [3,8], 'project_budget_cny':480, 'model_selection_calls':0, 'deployment_changed':False})
    print(json.dumps({'registered_runs':48, 'free_fixture_checks':len(checks), 'all_fixtures_passed':all(c['passed'] for c in checks)}))


def run():
    if (OUT/'summary.json').exists(): raise FileExistsError('Completed results are frozen')
    registration = read(OUT/'registration.json')
    assert all(sha(ROOT/name)==value for name,value in registration['source_sha256'].items())
    assert all(sha(OUT/name)==value for name,value in registration['initial_sha256'].items())
    assert sha(OUT/'cases.json')==registration['cases_sha256'] and sha(OUT/'fixture_checks.json')==registration['fixture_sha256']
    rows = {c['id']:c for c in read(OUT/'cases.json')}; records=[]
    gate = ProviderGate(ROOT/'evidence/provider_availability.sqlite')
    for ident,mode in registration['jobs']:
        case = rows[ident]; folder=OUT/'runs'/(ident+'-'+mode)
        if (folder/'result.json').exists(): records.append(read(folder/'result.json')); continue
        if folder.exists(): raise RuntimeError('Partial attempt requires explicit recovery; no silent repeat')
        client = guarded_business_client(gate); client.ensure_available(MODEL)
        folder.mkdir(); initial=OUT/'initial'/ident; seed=read(initial/'seed.json')
        store=clone(initial,folder/'operations.sqlite')
        assert digest(store.view(OWNER,seed['draft_id']))==seed['view_digest']
        cls=StateContractAgent if mode=='baseline_v3' else CandidateSearchAgent
        agent=cls(store,OWNER,seed['draft_id'],client=client,arm='single',now=instant(case['now']),
                  contract=case['contract'],phase='candidate_validation_v1_'+mode)
        save(folder/'attempt.json', {'case_id':ident,'mode':mode,'purpose':agent.purpose,'registration_sha256':sha(OUT/'registration.json')})
        execution=agent.run(case['task']); save(folder/'execution.json',execution)
        evaluation=evaluate(case,execution,store,seed)
        record={'case_id':ident,'family':case['family'],'mode':mode,'evaluation':evaluation,
                'execution_sha256':sha(folder/'execution.json'),**{k:execution[k] for k in
                ('run_status','error_type','model_calls','successful_model_calls','tool_calls','successful_tool_calls',
                 'input_tokens','output_tokens','accounted_and_reserved_cny','latency_seconds')}}
        save(folder/'result.json',record); records.append(record)
        print(json.dumps({'completed':len(records),'total':48,'case':ident,'family':case['family'],'mode':mode,
            'passed':evaluation['passed'],'failures':evaluation['failures'],'cny':record['accounted_and_reserved_cny']},ensure_ascii=False),flush=True)
    groups={}
    for mode in MODES:
        selected=[r for r in records if r['mode']==mode]; times=sorted(r['latency_seconds'] for r in selected)
        groups[mode]={'runs':len(selected),'passed':sum(r['evaluation']['passed'] for r in selected),
            'violations':sum(len(r['evaluation']['constraint_violations']) for r in selected),
            **{k:sum(r[k] for r in selected) for k in ('model_calls','successful_model_calls','tool_calls','successful_tool_calls','input_tokens','output_tokens')},
            'cost_cny':sum(float(r['accounted_and_reserved_cny']) for r in selected),
            'mean_latency_seconds':statistics.mean(times),'p95_latency_seconds':times[(95*len(times)+99)//100-1],
            'failures':dict(Counter(f for r in selected for f in r['evaluation']['failures'])),
            'families':{f:{'runs':sum(r['family']==f for r in selected),'passed':sum(r['family']==f and r['evaluation']['passed'] for r in selected)} for f in FAMILIES}}
    pair_counts=Counter()
    for ident in rows:
        pair={r['mode']:r['evaluation']['passed'] for r in records if r['case_id']==ident}
        pair_counts['win' if pair['candidate_any']>pair['baseline_v3'] else 'loss' if pair['candidate_any']<pair['baseline_v3'] else 'tie']+=1
    assert len(records)==48
    gate_passed=groups['candidate_any']['violations']==0 and pair_counts['win']>=pair_counts['loss'] and all(
        groups['candidate_any']['families'][f]['passed']>=groups['baseline_v3']['families'][f]['passed'] for f in
        ('shortage_alternative','shipping_new','shipping_infeasible','shipping_revision','approved_revision','valid_keep'))
    summary={'completed_at':datetime.now(timezone.utc).isoformat(),'registration_sha256':sha(OUT/'registration.json'),
        'groups':groups,'paired_cases':dict(pair_counts),'engineering_gate_passed':gate_passed,
        'result_sha256':{p.relative_to(OUT).as_posix():sha(p) for p in sorted((OUT/'runs').glob('*/result.json'))},
        'ledger_before':registration['ledger_before'],'ledger_after':ledger(),'new_real_users':0,'deployment_changed':False}
    save(OUT/'summary.json',summary)
    print(json.dumps({k:v for k,v in summary.items() if k!='result_sha256'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['prepare','run'])
    {'prepare':prepare,'run':run}[parser.parse_args().phase]()
