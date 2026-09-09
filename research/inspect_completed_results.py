"""Print recorded results and one real development trace without any model calls."""
from pathlib import Path
import json

ROOT=Path(__file__).resolve().parents[1]


def read(relative):
    return json.loads((ROOT/relative).read_text(encoding='utf-8-sig'))


def main():
    ranking=read('evidence/ranking_runs/20260907T130305394987Z_test_product/summary.json')
    business=read('evidence/final_business/20260907T144956405665Z/summary.json')
    reports=read('evidence/final_report_quality.json')
    validation=read('evidence/audit_replication_validation_gate.json')
    pause=read('evidence/audit_replication_pause.json')
    registration=read('evidence/audit_replication_test_registration.json')
    progress=json.loads((Path(registration['directory'])/'progress.json').read_text(encoding='utf-8-sig'))
    final_path=ROOT/'evidence/audit_replication_final_readout.json'
    final=json.loads(final_path.read_text(encoding='utf-8-sig')) if final_path.exists() else None
    comparison_path=ROOT/'evidence/ranking_compare/progress.json'
    comparison=json.loads(comparison_path.read_text(encoding='utf-8-sig')) if comparison_path.exists() else None
    demo=read('evidence/live_commerce_f2a5fdc245524956b149af594d77d595.json')
    all_ranking=ranking['results']['all']
    print(json.dumps({'recorded_results':{
        'ranking':{key:all_ranking[key] for key in ['bm25_candidate_pool','qwen_reranker','paired_ndcg10_delta']},
        'version_1_final':{arm:{'business_passed':item['passed'],'cases':item['cases'],
            'report_supported':reports['arms'][arm]['judge_supported'],'generation_cost_cny':item['settled_cost_cny'],
            'generation_latency_median_seconds':item['latency_median_seconds']} for arm,item in business['arms'].items()},
        'independent_validation':{'passed':validation['passed'],
            'arms':{arm:item['counts'] for arm,item in validation['arms'].items()}},
        'pause_record':{key:pause[key] for key in ['status','created_at','completed_trials','scheduled_trials','never_started_trials']},
        'replication_current_progress':progress,
        'replication_final':{'arms':{arm:item['counts'] for arm,item in final['arms'].items()}} if final else None,
        'small_model_comparison_progress':comparison,
        'scope':'Recorded evidence, not provider service status. Historical pause is separate from current progress. Candidate 6 remains rejected.'}},ensure_ascii=False,indent=2))
    steps=[]
    for trace in demo['traces']:
        if trace['kind']=='tool_result' and trace['payload']['name'] not in {'ask_catalog_agent','ask_logistics_agent'}:
            payload=trace['payload']
            steps.append({'role':trace['agent'],'tool':payload['name'],'arguments':payload['arguments'],
                          'output_type':type(payload['output']).__name__})
    result=demo['result']; confirmation=demo['host_confirmation']
    print(json.dumps({'development_example':{'run_id':result['run_id'],'model_calls':result['model_calls'],
        'generation_cost_cny':result['estimated_cost_cny'],'leaf_tool_steps':steps,
        'proposal_status':result['quote']['status'],'product_subtotal_usd':result['cart']['subtotal_usd'],
        'shipping_usd':result['quote']['plan']['total_cost_usd'],'shipping_days':result['quote']['plan']['transit_days'],
        'confirmation_actor':confirmation['actor'],'simulation_order_total_usd':confirmation['order']['total'],
        'idempotent':confirmation['idempotent'],'scope':'One preserved development example; no new model call or order mutation.'}},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
