"""Export measured comparisons and observations; never requests model inference."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import csv
import json
import math
import sqlite3

from model_selection_100 import budget100_run as run
from model_selection_100.prepare import ROOT, OUT as ORIGINAL, read, save, sha
from model_selection_100.validation_readout import audit_rows, validate_registration

THREAD = '01a07ab2-3c48-78c3-b86a-d470969b8e0e'
OUTPUT = ROOT.parents[1]/'outputs'/THREAD
SUPPORT = OUTPUT/'support'


def equivalent(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(equivalent(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equivalent(x, y) for x, y in zip(a, b))
    if type(a) in (int, float) and type(b) in (int, float):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
    return a == b


def frontier(groups):
    candidates = {m:s for m,s in groups.items() if s.get('overall_score') is not None}
    axes = [('ndcg_at_10',1),('accuracy',1),('effective_success_rate',1),
            ('avg_cost_cny',-1),('avg_latency_seconds',-1)]
    def dominates(a, b):
        return all(a[k]*direction >= b[k]*direction for k,direction in axes) and any(
            a[k]*direction > b[k]*direction for k,direction in axes)
    return {m: not any(dominates(other,s) for mid,other in candidates.items() if mid != m)
            for m,s in candidates.items()}


def generate():
    validate_registration()
    final = read(run.OUT/'validation_readout.json')
    if not final['complete']:
        raise ValueError('Independent validation is incomplete')
    if final['winner_selection_sha256'] != sha(run.OUT/'winner_selection.json'):
        raise ValueError('Winner registration changed')
    screen_selection = read(run.OUT/'shortlist_selection.json')
    shortlisted = screen_selection['models']; eligible = screen_selection['eligible']
    chosen = read(run.OUT/'winner_selection.json')
    all_summaries = {}; groups = []; observations = []; input_hashes = {}
    for stage in ('screen','shortlist','validation'):
        expected = read(run.OUT/(stage+'_summary.json'))['models']
        all_summaries[stage] = expected
        pareto = frontier(expected)
        for model in run.stage_models(stage):
            rows, computed, hashes, _ = audit_rows(stage, model, run.selected_queries(stage))
            if not equivalent(computed, expected[model['id']]):
                raise ValueError('Stage summary does not reproduce: '+stage+' '+model['id'])
            input_hashes.update(hashes)
            mid = model['id']; s = expected[mid]
            if stage == 'screen':
                status = '参照组进入复测' if mid == run.REFERENCE else (
                    '进入复测' if mid in shortlisted else '合格但未入前五' if mid in eligible else
                    '样本未完成' if not s['complete'] else '无有效答复' if not s['valid_responses'] else '未达晋级门槛')
            elif stage == 'shortlist':
                status = '预选赢家' if mid == chosen['winner'] else '参照组' if mid == run.REFERENCE else (
                    '合格但未胜出' if mid in chosen['eligible'] else '未达晋级门槛')
            else:
                status = '预选赢家' if mid == chosen['winner'] else '参照组'
            groups.append({'stage':stage,'model':model,'summary':s,'selection_status':status,
                           'pareto_frontier':pareto.get(mid),
                           'returned_models':sorted({str(r['response'].get('returned_model')) for r in rows if r['response'].get('returned_model')})})
            for row in rows:
                response=row['response']; u=response.get('usage',{}); t=response.get('timing',{})
                known=response.get('estimated_cost_cny')
                accounted=float(response['accounted_and_reserved_cny'])
                obs={'stage':stage,'model':mid,'query_id':str(row['query_id']),'locale':row['locale'],
                     'valid':int(row['valid_response']),
                     'ndcg_at_10':row['metrics']['ndcg_at_10'] if row['valid_response'] else 0,
                     'accuracy':row['metrics']['hit_exact_at_1'] if row['valid_response'] else 0,
                     'latency_seconds':response.get('latency_seconds'),
                     'latency_available':int(response.get('latency_seconds') is not None),
                     'known_cost_cny':float(known) if known is not None else None,
                     'accounted_and_reserved_cny':accounted,
                     'unknown_cost':int(known is None),
                     'prompt_tokens':u.get('prompt_tokens'),'completion_tokens':u.get('completion_tokens'),
                     'reasoning_tokens':u.get('reasoning_tokens'),'cached_tokens':u.get('cached_tokens'),
                     'ttft_seconds':t.get('ttft_seconds'),'time_to_first_answer_seconds':t.get('time_to_first_answer_seconds'),
                     'answer_stream_span_seconds':t.get('answer_stream_span_seconds'),
                     'observed_reasoning_span_seconds':t.get('observed_reasoning_span_seconds'),
                     'api_success':int(bool(response.get('api_success'))),'identity_match':int(bool(response.get('identity_match'))),
                     'status':row['status'],'returned_model':response.get('returned_model'),
                     'requested_at':response.get('requested_at'),'budget_call_id':response['budget_call_id'],
                     'source':model['source']}
                for k in ('prompt_tokens','completion_tokens','reasoning_tokens','cached_tokens',
                          'ttft_seconds','time_to_first_answer_seconds','answer_stream_span_seconds','observed_reasoning_span_seconds'):
                    obs[k+'_available']=int(obs[k] is not None)
                observations.append(obs)
    with closing(sqlite3.connect(ROOT/'evidence/api_budget.sqlite')) as db:
        rows = db.execute("SELECT purpose,reserved,charged,status FROM calls WHERE purpose LIKE 'model-selection-100:%'").fetchall()
    budget={'task_limit_cny':100,'whole_project_limit_cny':480,
            'requests':len(rows),'settled_estimate_cny':sum(r[2] or 0 for r in rows)/1e6,
            'uncertain_reserved_cny':sum(r[1] for r in rows if r[2] is None)/1e6,
            'accounted_and_reserved_cny':sum(r[2] if r[2] is not None else r[1] for r in rows)/1e6,
            'unknown_cost_calls':sum(r[2] is None for r in rows),
            'calibration_calls':sum(r[0].startswith('model-selection-100:calibration') for r in rows)}
    if budget['accounted_and_reserved_cny'] > 100 or budget['requests'] != len(observations)+budget['calibration_calls']:
        raise ValueError('Task budget or request counts do not reconcile')
    groups.sort(key=lambda g:({'screen':0,'shortlist':1,'validation':2}[g['stage']],
                             -(g['summary'].get('overall_score') or -1),g['model']['id']))
    result={'created_at':datetime.now(timezone.utc).isoformat(),'budget':budget,'groups':groups,
            'observations':observations,'final':final,'input_hashes':input_hashes,
            'currency_assumption_cny_per_usd':8,
            'sources':[
                ['ESCI数据与标注候选池','https://github.com/amazon-science/esci-data'],
                ['RouterBench费用质量方法','https://arxiv.org/html/2403.12031v2'],
                ['HELM多指标评估','https://arxiv.org/abs/2211.09110'],
                ['RankLLM重排工具','https://github.com/castorini/rank_llm'],
                ['vLLM时延定义','https://docs.vllm.ai/en/v0.10.2/design/metrics.html'],
                ['本次服务接口','https://docs.aihubmix.com/cn/api/unified-inference']]}
    save(SUPPORT/'model_comparison_data.json',result)
    fields=['model','family','configuration','attempted_queries','expected_queries','valid_responses',
            'ndcg_at_10','accuracy','avg_cost_cny','avg_latency_seconds','effective_success_rate','overall_score',
            'avg_prompt_tokens','avg_completion_tokens','avg_reasoning_tokens','avg_cached_tokens',
            'p50_latency_seconds','p95_latency_seconds','api_success_rate','failure_rate','unknown_cost_calls',
            'known_cost_cny','accounted_and_reserved_cny','quality_first_score','economy_first_score',
            'aiq','pareto_frontier','selection_status','failure_reasons','source']
    with (OUTPUT/'100模型初筛实测.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for g in groups:
            if g['stage']!='screen':continue
            s=g['summary'];m=g['model'];record={k:s.get(k) for k in fields}
            record.update(model=m['id'],family=m['family'],configuration=m['reasoning_effort'],source=m['source'],
                          quality_first_score=s.get('sensitivity',{}).get('quality_first'),
                          economy_first_score=s.get('sensitivity',{}).get('economy_first'),aiq=s.get('cost_quality_aiq'),
                          pareto_frontier=g['pareto_frontier'],selection_status=g['selection_status'],
                          failure_reasons=json.dumps(s.get('failure_reasons',{}),ensure_ascii=False))
            writer.writerow(record)
    print({'models':100,'observations':len(observations),'budget':budget,'output_directory':str(OUTPUT)})
    return result


if __name__ == '__main__': generate()
