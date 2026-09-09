"""Read-only independent evidence reconciliation for the v4/v5 comparison."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics

from research.audit_apparel_candidate_validation import connect, digest, micro, read, route_check, sha, at, pointer

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'evidence/apparel_source_validation_v1'
OUT=ROOT/'evidence/apparel_source_validation_audit_20260909.json'


def main():
    if OUT.exists():raise FileExistsError('Preserve completed audit')
    reg,summary=read(DATA/'registration.json'),read(DATA/'summary.json')
    verified={}
    def verify(path,expected):
        assert sha(path)==expected,str(path)
        verified[path.relative_to(ROOT).as_posix()]=expected
    verify(DATA/'registration.json',summary['registration_sha256'])
    verify(DATA/'cases.json',reg['cases_sha256']);verify(DATA/'fixture_checks.json',reg['fixture_sha256'])
    for name,value in reg['source_sha256'].items():verify(ROOT/name,value)
    for name,value in reg['initial_sha256'].items():verify(DATA/name,value)
    for name,value in summary['result_sha256'].items():verify(DATA/name,value)
    cases={c['id']:c for c in read(DATA/'cases.json')}
    assert len(cases)==24 and len(reg['jobs'])==48 and len(set(map(tuple,reg['jobs'])))==48
    assert all(f['passed'] for f in read(DATA/'fixture_checks.json'))
    corridor=read(ROOT/'data/apparel_corridor_v1.json')
    groups={mode:Counter() for mode in summary['groups']};latencies={m:[] for m in groups}
    failures={m:Counter() for m in groups};records=[];ids=set();requested=set();returned=set()
    with closing(connect(ROOT/'evidence/api_budget.sqlite')) as budget:
        for ident,mode in reg['jobs']:
            case=cases[ident];count=groups[mode];folder=DATA/'runs'/(ident+'-'+mode)
            result,execution,attempt=(read(folder/name) for name in ('result.json','execution.json','attempt.json'))
            verify(folder/'execution.json',result['execution_sha256'])
            assert attempt['registration_sha256']==sha(DATA/'registration.json')
            before,after,expected=execution['before'],execution['after'],case['expected']
            seed=read(DATA/'initial'/ident/'seed.json')
            assert digest(before)==seed['view_digest']
            assert before['request']==after['request']==case['request']
            assert before['approved_substitutions']==after['approved_substitutions']
            assert before['confirmation']==after['confirmation'] is None
            assert execution['run_status']=='completed' and not execution['delegations']
            selected={p['line_id']:p['sku'] for p in after['selections']}
            assert len(selected)==len(after['selections']) and set(selected)==set(expected['allowed'])
            assert all(sku in expected['allowed'][line] for line,sku in selected.items())
            decision=execution['report']['decision']
            pairs=lambda rows:sorted((p['line_id'],p['sku']) for p in rows)
            assert pairs(decision['selection_snapshot'])==pairs(after['selections'])
            assert set(decision['product_skus'])==set(selected.values())
            assert after['order_check']['status']==expected['order_status'] and decision['status']==expected['decision_status']
            if expected['read_only']:assert all(before[k]==after[k] for k in ('selections','revision','proposals'))
            if expected.get('issue'):assert expected['issue'] in {i['code'] for i in after['order_check']['issues']}
            with closing(connect(folder/'operations.sqlite')) as db:
                persisted=db.execute('SELECT request,selections,revision FROM drafts WHERE id=?',(after['id'],)).fetchone()
                assert (json.loads(persisted[0]),json.loads(persisted[1]),persisted[2])==(after['request'],after['selections'],after['revision'])
                approvals=[json.loads(r[0]) for r in db.execute('SELECT payload FROM approvals')]
                assert sorted(approvals,key=lambda x:x['approval_id'])==sorted(after['approved_substitutions'],key=lambda x:x['approval_id'])
                assert db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0]==0
                proposals=[json.loads(r[1])|{'state':r[0]} for r in db.execute('SELECT state,payload FROM proposals ORDER BY version')]
                assert proposals==after['proposals']
                assert dict(db.execute('SELECT sku,quantity FROM inventory'))=={sku:s['available_catalog_units'] for sku,s in case['world']['stock'].items()}
                events=[json.loads(r[0]) for r in db.execute('SELECT payload FROM transport_events ORDER BY event_id')]
                assert digest(events)==seed['events_digest']
            tools=[t for t in execution['traces'] if t['kind']=='tool']
            assert all(t['success'] for t in tools) and len(tools)==execution['tool_calls']==execution['successful_tool_calls']
            assert set(expected['required_tools'])<={t['tool'] for t in tools}
            reads={t['result']['variant']['sku'] for t in tools if t['tool']=='read_variant'}
            missing=expected['read_variant'] and not set(selected.values())<=reads
            measured_failures=['source_read'] if missing else []
            assert result['evaluation']['failures']==measured_failures and result['evaluation']['passed']==(not missing)
            assert [k for k,v in result['evaluation']['checks'].items() if not v]==measured_failures
            assert not result['evaluation']['constraint_violations']
            material_matches={}
            if expected['read_variant']:
                for sku in sorted(set(selected.values())):
                    wanted={'variant':case['world']['variants'][sku],'stock':case['world']['stock'][sku],
                            'brand_rule':case['world']['brand_rules'][case['world']['variants'][sku]['brand']]}
                    for trace in tools:
                        if trace['tool']!='read_variant':continue
                        material={k:trace['result'].get(k) for k in wanted}
                        material['variant']=dict(material['variant'] or {})
                        truncated=material['variant'].pop('description_excerpt_truncated',False)
                        if not truncated and material==wanted:
                            assert digest(material['variant']['source_record'])==material['variant']['source_record_sha256']
                            material_matches[sku]={'observation_id':trace['observation_id'],'material_sha256':digest(wanted)}
                assert result['current_material']['matched']=={sku:r['observation_id'] for sku,r in material_matches.items()}
                assert result['current_material']['passed']==(set(material_matches)==set(selected.values()))
            else:assert not result['current_material']['required'] and result['current_material']['passed']
            if mode=='v5_source_review':
                status=execution['report']['source_review']
                assert status==execution['source_review']==execution['report']['execution_receipt']['source_review']
                assert status['passed'] and not status['missing']
                assert set(status['required_skus'])==(set(selected.values()) if expected['read_variant'] else set())
                for receipt in status['receipts']:
                    match=material_matches[receipt['sku']]
                    assert receipt['observation_id']==match['observation_id'] and receipt['material_sha256']==match['material_sha256']
            rejections=sum(t['kind']=='source_check' and bool(t['errors']) for t in execution['traces'])
            assert rejections==result['source_submission_rejections']
            searches=[t for t in tools if t['tool']=='search_variants'];queries=[]
            for trace in searches:
                args,value=trace['arguments'],trace['result']
                for variant in value['variants']:
                    original=case['world']['variants'][variant['sku']]
                    assert all(args.get(k) is None or str(original[k]).casefold()==str(args[k]).casefold() for k in ('brand','color','size','style_id'))
                    assert variant['source_record_sha256']==original['source_record_sha256']
                method=value['retrieval']['method']
                count['gpu_requests']+=method=='local_qwen_rerank'
                if method=='local_qwen_rerank':count['gpu_pairs']+=value['retrieval']['candidate_count']
                count['fallbacks']+='fallback' in method
                count['empty_searches']+=not value['variants']
                queries.append({'arguments':args,'returned':len(value['variants']),'method':method})
            if expected.get('empty_search'):
                assert any(t['arguments'].get('brand')==case['request']['lines'][0]['brand'] and not t['result']['variants'] for t in searches)
            report=execution['report']
            for fact in report['source_facts']:
                obs=execution['observations'][fact['observation_id']]
                assert obs['success'] and fact['tool']==obs['tool'] and pointer(obs,fact['pointer'])==fact['value']
            assert report['grounding']['supported']==report['grounding']['total']==len(report['source_facts']) and not report['grounding']['invalid']
            action=expected['proposal']
            assert len(after['proposals'])==len(before['proposals'])+int(action in ('new','revise','infeasible'))
            if action!='none':
                proposal=after['proposals'][-1]
                assert decision['proposal_id']==proposal['proposal_id']
                if action=='infeasible':
                    assert proposal['state']=='needs_adjustment' and proposal['route']['status']=='infeasible'
                    assert proposal['route']['adjustment_options'] and all(x['requires_user_choice'] for x in proposal['route']['adjustment_options'])
                    shipping=case['request']['shipping']
                    assert shipping['budget_cents']==1 or (at(shipping['deadline_at'])-at(shipping['ready_at'])).total_seconds()==3600
                    assert all(l['fixed_cents']>1 and l['duration_minutes']>60 for l in corridor['legs'])
                    count['infeasible_checks']+=1
                elif case['request']['needs_shipping']:
                    weight=sum(case['world']['variants'][p['sku']]['weight_grams_per_catalog_unit']*proposal['order_check']['quantities_catalog_units'][p['sku']] for p in after['selections'])
                    route_check(proposal['route'],case['request'],weight,corridor,events,at(case['now']))
                    assert proposal['state']=='pending' and proposal['independent_route_audit']['passed']
                    count['itinerary_checks']+=1
                else:assert proposal['route']=={'segments':[],'status':'not_required','total_cost_cents':0} and proposal['state']=='pending'
                if action=='keep':assert after['proposals']==before['proposals']
                if action=='revise':assert proposal['previous_proposal_id']==seed['old_id'] and proposal['version']==before['proposals'][-1]['version']+1 and after['proposals'][-2]['state']=='superseded'
            if seed['old_id']:
                assert any(t['tool']=='read_proposal' and t['result']['proposal']['proposal_id']==seed['old_id'] and t['result']['validity']['valid']==expected['old_valid'] for t in tools)
            paid=0
            for number,call in enumerate(execution['calls'],1):
                ident_call=call['budget_call_id'];assert ident_call not in ids and call['status']=='success';ids.add(ident_call)
                row=budget.execute('SELECT purpose,status,usage,charged FROM calls WHERE id=?',(ident_call,)).fetchone()
                assert row[0]==attempt['purpose']+str(number) and row[1]=='settled' and json.loads(row[2])==call['usage'] and row[3]==micro(call['estimated_cost_cny'])
                requested.add(call['requested_model']);returned.add(call['returned_model']);paid+=row[3]
            assert len(execution['calls'])==execution['model_calls']==execution['successful_model_calls']
            assert paid==micro(execution['accounted_and_reserved_cny'])==micro(result['accounted_and_reserved_cny'])
            for metric,usage in (('input_tokens','prompt_tokens'),('output_tokens','completion_tokens')):
                assert sum(call['usage'][usage] for call in execution['calls'])==execution[metric]==result[metric]
                count[metric]+=execution[metric]
            count.update(runs=1,passed=not missing,model_calls=execution['model_calls'],tool_calls=len(tools),
                host_initial_reads=execution['host_initial_reads'],cost_micro_cny=paid,search_calls=len(searches),
                runs_with_search=bool(searches),source_read_required=expected['read_variant'],source_read_failures=missing,
                source_submission_rejections=rejections)
            failures[mode].update(measured_failures);latencies[mode].append(execution['latency_seconds'])
            records.append({'case_id':ident,'family':case['family'],'mode':mode,'passed':not missing,'failures':measured_failures,
                'material_matches':material_matches,'queries':queries,'model_calls':execution['model_calls'],'tool_calls':len(tools),
                'cost_micro_cny':paid,'latency_seconds':execution['latency_seconds'],'source_submission_rejections':rejections})
        purpose_ids={r[0] for r in budget.execute('SELECT id FROM calls WHERE purpose LIKE ?',('commerce_apparel:source_validation_v1_%',))}
        assert purpose_ids==ids
        total,rows=budget.execute('SELECT SUM(COALESCE(charged,reserved)),COUNT(*) FROM calls').fetchone()
    for mode,count in groups.items():
        expected=summary['groups'][mode]
        for key in ('runs','passed','model_calls','tool_calls','input_tokens','output_tokens','source_submission_rejections'):assert count[key]==expected[key]
        assert count['cost_micro_cny']==micro(expected['cost_cny']) and dict(failures[mode])==expected['failures']
        assert statistics.mean(latencies[mode])==expected['mean_latency_seconds']
        assert sorted(latencies[mode])[(95*len(latencies[mode])+99)//100-1]==expected['p95_latency_seconds']
    cost=sum(c['cost_micro_cny'] for c in groups.values())
    assert summary['ledger_after']['micro_cny']-summary['ledger_before']['micro_cny']==cost
    assert summary['ledger_after']['rows']-summary['ledger_before']['rows']==len(ids)
    assert (total,rows)==(summary['ledger_after']['micro_cny'],summary['ledger_after']['rows'])
    output={'created_at':datetime.now(timezone.utc).isoformat(),'passed':True,'groups':{m:dict(c) for m,c in groups.items()},
        'cases':records,'unique_successful_paid_calls':len(ids),'paid_micro_cny':cost,'ledger_micro_cny':total,'ledger_rows':rows,
        'requested_models':sorted(requested),'returned_models':sorted(returned),'verified_sha256':verified,
        'new_model_calls_during_audit':0,'new_real_users':0,'deployment_changed':False,
        'scope':'Raw evidence and exact ledger reconciliation, SQLite state, current material equality and separately implemented itinerary arithmetic.',
        'limits':['Known 37-product directory and 12 developer-defined template families, one trajectory per mode/case.',
            'Material equality confirms recorded exposure and freshness, not comprehension of every sentence or real stock.',
            'Route arithmetic proves feasibility of returned itineraries, not globally cheapest service or carrier availability.',
            'Local conservative fees and provider model aliases, not independent weights verification or a supplier invoice.']}
    with OUT.open('x',encoding='utf-8') as f:f.write(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in output.items() if k not in ('cases','verified_sha256')},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
