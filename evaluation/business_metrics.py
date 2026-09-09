"""Predeclared paired, product-clustered summaries for the synthetic task set."""
from collections import Counter
from decimal import Decimal
import random
import statistics


def quantile(values, p):
    if not values:
        return None
    values=sorted(values)
    position=(len(values)-1)*p
    low=int(position)
    return values[low]+(values[min(low+1,len(values)-1)]-values[low])*(position-low)


def summarize(rows):
    latencies=[r['latency_seconds'] for r in rows if 'latency_seconds' in r]
    return {'cases':len(rows),'passed':sum(r['score']['passed'] for r in rows),
        'run_statuses':dict(Counter(r['run_status'] for r in rows)),
        'grading_errors':sum('evaluation_error' in r['score'] for r in rows),
        'settled_cost_cny':str(sum((Decimal(r.get('settled_cost_cny','0')) for r in rows),Decimal(0))),
        'model_calls':sum(r.get('model_calls',0) for r in rows),
        'latency_median_seconds':statistics.median(latencies) if latencies else None,
        'latency_p95_seconds':quantile(latencies,.95),
        'by_family':{f:{'cases':sum(r['family']==f for r in rows),
            'passed':sum(r['family']==f and r['score']['passed'] for r in rows)} for f in sorted({r['family'] for r in rows})}}


def paired(baseline, candidate, cases, *, repetitions=2000):
    left={r['case_id']:r for r in baseline}; right={r['case_id']:r for r in candidate}
    expected={c['id'] for c in cases}
    if len(left)!=len(baseline) or len(right)!=len(candidate) or set(left)!=expected or set(right)!=expected:
        raise ValueError('A paired summary requires every case exactly once in both arms')
    groups={}
    for case in cases:
        groups.setdefault(case['product_id'],[]).append(int(right[case['id']]['score']['passed'])-int(left[case['id']]['score']['passed']))
    clusters=[groups[k] for k in sorted(groups)]
    rng=random.Random(20260907)
    samples=[]
    for _ in range(repetitions):
        draw=[rng.choice(clusters) for _ in clusters]
        samples.append(sum(sum(g) for g in draw)/sum(len(g) for g in draw))
    return {'cases':len(cases),'product_groups':len(groups),
        'pass_rate_difference':sum(sum(g) for g in clusters)/len(cases),
        'cluster_percentile_95_interval':[quantile(samples,.025),quantile(samples,.975)],
        'improved':sorted(i for i in expected if right[i]['score']['passed'] and not left[i]['score']['passed']),
        'regressed':sorted(i for i in expected if left[i]['score']['passed'] and not right[i]['score']['passed']),
        'bootstrap_repetitions':repetitions,'seed':20260907,
        'scope':'20 synthetic product groups sharing templates; not real-customer generalization or an official leaderboard score.'}
