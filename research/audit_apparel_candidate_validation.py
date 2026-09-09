"""Read frozen files and SQLite only; do not call models or rescore failed runs."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evidence/apparel_candidate_validation_v1'
OUT = ROOT / 'evidence/apparel_candidate_validation_audit_20260909.json'


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


def connect(path):
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)


def at(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def pointer(value, path):
    for token in path.lstrip('/').split('/'):
        token = token.replace('~1', '/').replace('~0', '~')
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def route_check(route, request, weight, corridor, events, now):
    """Separate timestamp, capacity and arithmetic checks of a returned itinerary."""
    assert route['status'] == 'planned'
    assert route['shipping_constraints'] == request['shipping']
    assert route['weight_grams'] == weight
    assert route['corridor_digest'] == digest(corridor)
    active = [e for e in events if at(e['published_at']) <= now]
    location, arrival, cost = corridor['origin'], at(request['shipping']['ready_at']), 0
    legs = {leg['id']: leg for leg in corridor['legs']}
    for segment in route['segments']:
        leg = legs[segment['leg_id']]
        nominal, departure, end = map(at, (segment['nominal_departure'], segment['departure_at'], segment['arrival_at']))
        assert (nominal - at(corridor['anchor_at'])).total_seconds() / 60 % leg['period_minutes'] == leg['offset_minutes']
        relevant = [e for e in active if e['leg_id'] == leg['id'] and at(e['nominal_departure']) == nominal]
        assert not any(e['kind'] == 'cancel' for e in relevant)
        delay = sum(e.get('delay_minutes', 0) for e in relevant if e['kind'] == 'delay')
        assert departure == nominal + timedelta(minutes=delay)
        assert end == departure + timedelta(minutes=leg['duration_minutes'])
        assert segment['origin'] == leg['origin'] == location and segment['destination'] == leg['destination']
        assert segment['mode'] == leg['mode'] and weight <= leg['capacity_grams']
        transfer = corridor['transfer_minutes'][location]
        assert segment['transfer_minutes'] == transfer and departure >= arrival + timedelta(minutes=transfer)
        # Recorded wait is the full dwell interval, including minimum transfer time.
        assert segment['wait_minutes'] == (departure-arrival).total_seconds()/60
        expected_cost = leg['fixed_cents'] + (weight * leg['per_kg_cents'] + 999) // 1000
        assert segment['cost_cents'] == expected_cost
        assert set(segment['event_ids']) == {e['event_id'] for e in relevant}
        cost += expected_cost
        location, arrival = leg['destination'], end
    assert location == request['shipping']['destination'] == corridor['destination']
    assert at(route['arrival_at']) == arrival <= at(request['shipping']['deadline_at'])
    assert route['total_cost_cents'] == cost <= request['shipping']['budget_cents']


def main():
    if OUT.exists():
        raise FileExistsError('Preserve the completed audit')
    reg, summary = read(DATA/'registration.json'), read(DATA/'summary.json')
    checked = {}

    def verify(path, expected):
        actual = sha(path)
        assert actual == expected, str(path)
        checked[path.relative_to(ROOT).as_posix()] = actual

    verify(DATA/'registration.json', summary['registration_sha256'])
    verify(DATA/'cases.json', reg['cases_sha256'])
    verify(DATA/'fixture_checks.json', reg['fixture_sha256'])
    for name, value in reg['source_sha256'].items(): verify(ROOT/name, value)
    for name, value in reg['initial_sha256'].items(): verify(DATA/name, value)
    for name, value in summary['result_sha256'].items(): verify(DATA/name, value)
    cases = {c['id']: c for c in read(DATA/'cases.json')}
    fixtures = read(DATA/'fixture_checks.json')
    assert len(cases) == len(fixtures) == 24 and all(f['passed'] for f in fixtures)
    assert len(reg['jobs']) == len(set(map(tuple, reg['jobs']))) == 48
    corridor = read(ROOT/'data/apparel_corridor_v1.json')
    groups = {mode: Counter() for mode in summary['groups']}
    latencies = {mode: [] for mode in groups}
    failures = {mode: Counter() for mode in groups}
    case_records, call_ids, requested, returned = [], set(), set(), set()
    with closing(connect(ROOT/'evidence/api_budget.sqlite')) as budget:
        budget.row_factory = sqlite3.Row
        for ident, mode in reg['jobs']:
            case, count = cases[ident], groups[mode]
            folder = DATA/'runs'/(ident+'-'+mode)
            execution, result, attempt = (read(folder/name) for name in ('execution.json','result.json','attempt.json'))
            verify(folder/'execution.json', result['execution_sha256'])
            assert attempt['registration_sha256'] == sha(DATA/'registration.json')
            assert attempt['case_id'] == ident and attempt['mode'] == mode
            before, after, expected = execution['before'], execution['after'], case['expected']
            seed = read(DATA/'initial'/ident/'seed.json')
            assert digest(before) == seed['view_digest']
            assert before['request'] == after['request'] == case['request']
            assert before['approved_substitutions'] == after['approved_substitutions']
            assert before['confirmation'] == after['confirmation'] is None
            assert execution['run_status'] == 'completed'
            assert execution['operation_contract'] == case['contract'] or all(
                execution['operation_contract'][k] == v for k,v in case['contract'].items())
            assert not execution['delegations']
            selected = {p['line_id']: p['sku'] for p in after['selections']}
            assert len(selected) == len(after['selections']) and set(selected) == set(expected['allowed'])
            assert all(sku in expected['allowed'][line] for line,sku in selected.items())
            decision = execution['report']['decision']
            pairs = lambda rows: sorted((p['line_id'],p['sku']) for p in rows)
            assert pairs(decision['selection_snapshot']) == pairs(after['selections'])
            assert set(decision['product_skus']) == set(selected.values())
            assert after['order_check']['status'] == expected['order_status']
            assert decision['status'] == expected['decision_status']
            if expected['read_only']:
                assert all(after[k] == before[k] for k in ('selections','revision','proposals'))
            if expected.get('issue'):
                assert expected['issue'] in {i['code'] for i in after['order_check']['issues']}
            with closing(connect(folder/'operations.sqlite')) as db:
                persisted = db.execute('SELECT request,selections,revision FROM drafts WHERE id=?',(after['id'],)).fetchone()
                assert (json.loads(persisted[0]),json.loads(persisted[1]),persisted[2]) == (after['request'],after['selections'],after['revision'])
                approvals = [json.loads(row[0]) for row in db.execute('SELECT payload FROM approvals')]
                assert sorted(approvals,key=lambda x:x['approval_id']) == sorted(after['approved_substitutions'],key=lambda x:x['approval_id'])
                assert db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0] == 0
                proposals = [json.loads(row[1]) | {'state':row[0]} for row in db.execute('SELECT state,payload FROM proposals ORDER BY version')]
                assert proposals == after['proposals']
                assert dict(db.execute('SELECT sku,quantity FROM inventory')) == {k:v['available_catalog_units'] for k,v in case['world']['stock'].items()}
                events = [json.loads(row[0]) for row in db.execute('SELECT payload FROM transport_events ORDER BY event_id')]
                assert digest(events) == seed['events_digest']
            tools = [t for t in execution['traces'] if t['kind']=='tool']
            assert all(t['success'] for t in tools) and len(tools) == execution['tool_calls'] == execution['successful_tool_calls']
            names = {t['tool'] for t in tools}
            assert set(expected['required_tools']) <= names
            read_ids = {t['result']['variant']['sku'] for t in tools if t['tool']=='read_variant'}
            source_missing = expected['read_variant'] and not set(selected.values()) <= read_ids
            measured_failures = ['source_read'] if source_missing else []
            assert result['evaluation']['failures'] == measured_failures
            assert result['evaluation']['passed'] == (not measured_failures)
            assert result['evaluation']['constraint_violations'] == []
            assert [k for k,v in result['evaluation']['checks'].items() if not v] == measured_failures
            searches = [t for t in tools if t['tool']=='search_variants']
            queries = []
            for search in searches:
                args, value = search['arguments'], search['result']
                ids = [v['sku'] for v in value['variants']]
                assert len(ids) == len(set(ids)) and set(ids) <= set(case['world']['variants'])
                for candidate in value['variants']:
                    variant = case['world']['variants'][candidate['sku']]
                    assert all(args.get(k) is None or str(variant[k]).casefold()==str(args[k]).casefold() for k in ('brand','color','size','style_id'))
                    if mode == 'candidate_any':
                        assert candidate['source_record_sha256'] == variant['source_record_sha256'] == digest(variant['source_record'])
                        assert candidate['field_provenance'] == variant['provenance']
                retrieval = value.get('retrieval',{'method':'baseline_substring'})
                method = retrieval['method']
                count['gpu_requests'] += method == 'local_qwen_rerank'
                if method == 'local_qwen_rerank': count['gpu_pairs'] += retrieval['candidate_count']
                count['fallbacks'] += bool(retrieval.get('fallback')) or 'fallback' in method
                count['empty_searches'] += not ids
                count['empty_query_browse'] += not args.get('query')
                queries.append({'arguments':args,'returned':len(ids),'method':method})
            if expected.get('empty_search'):
                assert any(t['arguments'].get('brand')==case['request']['lines'][0]['brand'] and not t['result']['variants'] for t in searches)
            report = execution['report']
            for fact in report['source_facts']:
                obs = execution['observations'][fact['observation_id']]
                assert obs['success'] and fact['tool'] == obs['tool']
                assert pointer(obs,fact['pointer']) == fact['value']
            assert report['grounding']['supported'] == report['grounding']['total'] == len(report['source_facts'])
            assert not report['grounding']['invalid']
            action = expected['proposal']
            assert len(after['proposals']) == len(before['proposals'])+int(action in ('new','revise','infeasible'))
            if action != 'none':
                proposal = after['proposals'][-1]
                assert decision['proposal_id'] == proposal['proposal_id']
                if action == 'infeasible':
                    assert proposal['state']=='needs_adjustment' and proposal['route']['status']=='infeasible'
                    assert proposal['route']['adjustment_options'] and all(x['requires_user_choice'] for x in proposal['route']['adjustment_options'])
                    shipping=case['request']['shipping']
                    assert shipping['budget_cents']==1 or (at(shipping['deadline_at'])-at(shipping['ready_at'])).total_seconds()==3600
                    assert all(l['fixed_cents']>1 and l['duration_minutes']>60 for l in corridor['legs'])
                    count['infeasible_checks'] += 1
                elif case['request']['needs_shipping']:
                    weight=sum(case['world']['variants'][p['sku']]['weight_grams_per_catalog_unit']*proposal['order_check']['quantities_catalog_units'][p['sku']] for p in after['selections'])
                    route_check(proposal['route'],case['request'],weight,corridor,events,at(case['now']))
                    count['itinerary_checks'] += 1
                    assert proposal['state']=='pending' and proposal['independent_route_audit']['passed']
                else:
                    assert proposal['route']=={'segments':[],'status':'not_required','total_cost_cents':0} and proposal['state']=='pending'
                if action=='revise':
                    assert proposal['previous_proposal_id']==seed['old_id'] and proposal['version']==before['proposals'][-1]['version']+1
                    assert after['proposals'][-2]['state']=='superseded'
                if action=='keep': assert after['proposals']==before['proposals']
            if seed['old_id']:
                assert any(t['tool']=='read_proposal' and t['result']['proposal']['proposal_id']==seed['old_id'] and t['result']['validity']['valid']==expected['old_valid'] for t in tools)
            paid = 0
            for number, call in enumerate(execution['calls'],1):
                ident_call = call['budget_call_id']
                assert ident_call not in call_ids and call['status']=='success'
                call_ids.add(ident_call)
                row=budget.execute('SELECT * FROM calls WHERE id=?',(ident_call,)).fetchone()
                assert row and row['status']=='settled' and row['purpose']==attempt['purpose']+str(number)
                assert json.loads(row['usage'])==call['usage'] and row['charged']==micro(call['estimated_cost_cny'])
                requested.add(call['requested_model']); returned.add(call['returned_model'])
                paid += row['charged']
            assert len(execution['calls'])==execution['model_calls']==execution['successful_model_calls']
            assert paid==micro(execution['accounted_and_reserved_cny'])==micro(result['accounted_and_reserved_cny'])
            for metric, usage in (('input_tokens','prompt_tokens'),('output_tokens','completion_tokens')):
                assert sum(call['usage'][usage] for call in execution['calls'])==execution[metric]==result[metric]
                count[metric] += execution[metric]
            count.update(runs=1,passed=not measured_failures,model_calls=execution['model_calls'],tool_calls=len(tools),
                         host_initial_reads=execution['host_initial_reads'],cost_micro_cny=paid,search_calls=len(searches),
                         runs_with_search=bool(searches),source_read_failures=source_missing,source_read_required=expected['read_variant'])
            failures[mode].update(measured_failures)
            latencies[mode].append(execution['latency_seconds'])
            case_records.append({'case_id':ident,'family':case['family'],'mode':mode,'passed':not measured_failures,
                'failures':measured_failures,'selected':after['selections'],'read_variant_ids':sorted(read_ids),
                'queries':queries,'model_calls':execution['model_calls'],'tool_calls':len(tools),'cost_micro_cny':paid,
                'latency_seconds':execution['latency_seconds'],'proposal_action':action})
        all_rows=budget.execute('SELECT id FROM calls WHERE purpose LIKE ?',('commerce_apparel:candidate_validation_v1_%',)).fetchall()
        assert {r['id'] for r in all_rows}==call_ids
        total, rows=budget.execute('SELECT SUM(COALESCE(charged,reserved)),COUNT(*) FROM calls').fetchone()
    for mode,count in groups.items():
        expected=summary['groups'][mode]
        for metric in ('runs','passed','model_calls','tool_calls','input_tokens','output_tokens'): assert count[metric]==expected[metric]
        assert count['cost_micro_cny']==micro(expected['cost_cny']) and dict(failures[mode])==expected['failures']
        assert statistics.mean(latencies[mode])==expected['mean_latency_seconds']
        assert sorted(latencies[mode])[(95*len(latencies[mode])+99)//100-1]==expected['p95_latency_seconds']
    new_cost=sum(c['cost_micro_cny'] for c in groups.values())
    assert summary['ledger_after']['micro_cny']-summary['ledger_before']['micro_cny']==new_cost
    assert summary['ledger_after']['rows']-summary['ledger_before']['rows']==len(call_ids)
    assert (total,rows)==(summary['ledger_after']['micro_cny'],summary['ledger_after']['rows'])
    pairs=Counter()
    for ident in cases:
        pair={r['mode']:r['passed'] for r in case_records if r['case_id']==ident}
        pairs['win' if pair['candidate_any']>pair['baseline_v3'] else 'loss' if pair['candidate_any']<pair['baseline_v3'] else 'tie']+=1
    assert dict(pairs)==summary['paired_cases']
    result={'created_at':datetime.now(timezone.utc).isoformat(),'passed':True,
        'scope':'Frozen results, exact ledger reconciliation, SQLite state and separate itinerary arithmetic. Original source-read failures remain failures.',
        'groups':{k:dict(v) for k,v in groups.items()},'cases':case_records,'paired_cases':dict(pairs),
        'unique_successful_paid_calls':len(call_ids),'paid_micro_cny':new_cost,'ledger_micro_cny':total,'ledger_rows':rows,
        'requested_models':sorted(requested),'returned_models':sorted(returned),'verified_sha256':checked,
        'new_model_calls_during_audit':0,'new_real_users':0,'deployment_changed':False,
        'audit_development_corrections':['Initial audit interpreted wait_minutes as dwell minus transfer. The stored field is total dwell including transfer; corrected the audit after reading its definition. No experiment, route, label or score changed.'],
        'limitations':['24 known-catalogue states, one model trajectory per mode; no unseen-product generalization.',
            'source_read is a pre-registered explicit read_variant process requirement. read_order already exposes partial variant fields.',
            'Exact source-fact pointer checks do not establish every free-text rationale claim or real carrier availability.',
            'Separate itinerary checks prove returned-route feasibility and arithmetic, not global optimality; no-route proof limited to 1-cent/1-hour fixtures.',
            'Task success has a different denominator from retrieval nDCG/top-one exact hit; no combined score.',
            'Provider model aliases and local conservative accounting retained; no independent weights verification or provider invoice.']}
    with OUT.open('x',encoding='utf-8') as f: f.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('cases','verified_sha256')},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
