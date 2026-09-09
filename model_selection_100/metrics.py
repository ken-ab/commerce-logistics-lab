"""Prespecified, auditable quality/cost/latency/reliability selection metrics."""
import json
import math
import re
from statistics import mean

from ranking_compare.experiment import score_order

WEIGHTS = {'quality':.70,'cost':.15,'latency':.10,'reliability':.05}


def decode_order(message, expected, finish_reason):
    if finish_reason != 'stop':
        raise ValueError('Incomplete or unexpected completion finish reason')
    text = message.get('content')
    if not isinstance(text,str):
        raise ValueError('A text JSON answer is required')
    # A single Markdown code fence is harmless formatting; no substring extraction.
    text = text.strip()
    if text.startswith('```'):
        match = re.fullmatch(r'```(?:json)?\s*\n?([\s\S]*?)\n?```',text,re.IGNORECASE)
        if not match: raise ValueError('Malformed JSON fence')
        text = match.group(1).strip()
    obj = json.loads(text)
    if not isinstance(obj,dict) or set(obj) != {'order'}:
        raise ValueError('Expected only an order field')
    order = obj['order']
    if (not isinstance(order,list) or any(type(x) is not str for x in order)
        or len(order)!=len(expected) or len(set(order))!=len(order) or set(order)!=set(expected)):
        raise ValueError('Ranking must be an exact permutation of the provided IDs')
    return order


def cost_factor(cost_per_1000, budget=5.0):
    if not math.isfinite(cost_per_1000) or cost_per_1000 < 0 or budget <= 0:
        raise ValueError('Cost must be finite and nonnegative')
    return 1-cost_per_1000/(2*budget) if cost_per_1000<=budget else budget/(2*cost_per_1000)


def score(ndcg, accuracy, avg_cost, avg_latency, success_rate, *, weights=None, cost_budget=5.0):
    weights = weights or WEIGHTS
    if not math.isclose(sum(weights.values()),1) or min(weights.values())<0:
        raise ValueError('Weights must form a probability simplex')
    values = [ndcg,accuracy,success_rate]
    if any(not math.isfinite(x) or not 0<=x<=1 for x in values) or not math.isfinite(avg_latency) or avg_latency<0:
        raise ValueError('Invalid measured quality, reliability or latency')
    q=(ndcg+accuracy)/2
    c=cost_factor(avg_cost*1000,cost_budget)
    latency=1.0 if avg_latency<=2 else 2/avg_latency
    total=100*(weights['quality']*q+weights['cost']*c+weights['latency']*latency+weights['reliability']*success_rate)
    return {'overall_score':total,'quality_score':100*q,'cost_score':100*c,
            'latency_score':100*latency,'reliability_score':100*success_rate,
            'cost_quality_aiq':100*q*c}


def quantile(xs,q):
    if not xs:return None
    xs=sorted(xs);p=(len(xs)-1)*q;i=int(p);j=min(i+1,len(xs)-1)
    return xs[i]*(j-p)+xs[j]*(p-i) if j!=i else xs[i]


def summarize(rows, expected):
    attempted=[r for r in rows if r.get('submitted')]
    n=len(attempted)
    valid=[r for r in attempted if r.get('valid_response')]
    result={'expected_queries':expected,'attempted_queries':n,'complete':n==expected,
            'valid_responses':len(valid),'unattempted_queries':expected-n}
    if not n:return result
    mkeys=('ndcg_at_10','hit_exact_at_1','mrr_exact')
    # Failure is zero in standalone model quality. Baseline fallback is separate.
    result.update({key:mean(r['metrics'][key] if r.get('valid_response') else 0 for r in attempted) for key in mkeys})
    result['accuracy']=result['hit_exact_at_1']
    result['valid_only_metrics']={k:mean(r['metrics'][k] for r in valid) for k in mkeys} if valid else None
    result['pipeline_with_fallback_metrics']={k:mean(r['pipeline_metrics'][k] for r in attempted) for k in mkeys}
    lat=[r['response']['latency_seconds'] for r in attempted if r['response'].get('latency_seconds') is not None]
    result.update(avg_latency_seconds=mean(lat) if lat else None,latency_coverage=len(lat)/n,p50_latency_seconds=quantile(lat,.5),p95_latency_seconds=quantile(lat,.95),
                  effective_success_rate=len(valid)/n,failure_rate=1-len(valid)/n,
                  api_success_rate=sum(bool(r['response'].get('api_success')) for r in attempted)/n,
                  accounted_and_reserved_cny=sum(float(r['response']['accounted_and_reserved_cny']) for r in attempted),
                  known_cost_cny=sum(float(r['response'].get('estimated_cost_cny') or 0) for r in attempted),
                  unknown_cost_calls=sum(r['response'].get('estimated_cost_cny') is None for r in attempted))
    result['avg_cost_cny']=result['accounted_and_reserved_cny']/n
    result['cost_cny_per_1000_queries']=result['avg_cost_cny']*1000
    result['cost_per_valid_answer_cny']=result['accounted_and_reserved_cny']/len(valid) if valid else None
    for key in ('prompt_tokens','completion_tokens','reasoning_tokens','cached_tokens'):
        values=[r['response'].get('usage',{}).get(key) for r in attempted]
        values=[v for v in values if v is not None]
        result['avg_'+key]=mean(values) if values else None
        result[key+'_coverage']=len(values)/n
    for key in ('ttft_seconds','time_to_first_answer_seconds','answer_stream_span_seconds','observed_reasoning_span_seconds'):
        values=[r['response'].get('timing',{}).get(key) for r in attempted]
        values=[v for v in values if v is not None]
        result['avg_'+key]=mean(values) if values else None
        result[key+'_coverage']=len(values)/n
    result['failure_reasons']={s:sum(r['status']==s for r in attempted) for s in sorted({r['status'] for r in attempted if not r.get('valid_response')})}
    result['identity_match_rate']=sum(bool(r['response'].get('identity_match')) for r in attempted)/n
    result['overall_score']=None
    if n==expected and valid and len(lat)==n:
        result.update(score(result['ndcg_at_10'],result['accuracy'],result['avg_cost_cny'],result['avg_latency_seconds'],result['effective_success_rate']))
        result['sensitivity']={name:score(result['ndcg_at_10'],result['accuracy'],result['avg_cost_cny'],result['avg_latency_seconds'],result['effective_success_rate'],weights=w)['overall_score']
          for name,w in {'quality_first':{'quality':.80,'cost':.10,'latency':.05,'reliability':.05},
                         'economy_first':{'quality':.60,'cost':.25,'latency':.10,'reliability':.05}}.items()}
        result['aiq_cost_interval_sensitivity']={str(b):100*(result['ndcg_at_10']+result['accuracy'])/2*cost_factor(result['avg_cost_cny']*1000,b) for b in (1,10)}
    return result
