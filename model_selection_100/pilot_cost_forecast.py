"""Cost-only forecast after the six prespecified common pilot cases."""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3
from statistics import mean

from model_selection_100.client import PROMPT
from model_selection_100.native_bound_run import OUT, validate, reconcile_accounting
from model_selection_100.prepare import ROOT, OUT as ORIGINAL, read, save, sha
from model_selection_100.run import selected_queries
from model_selection_100.calibrate import model_dir


def encoded_size(model, data):
    body = {'model': model['id'], 'messages': [{'role': 'system', 'content': PROMPT},
            {'role': 'user', 'content': json.dumps(data, ensure_ascii=False, separators=(',', ':'))}],
            'stream': True, 'stream_options': {'include_usage': True}, 'n': 1,
            'max_completion_tokens': 1024, 'reasoning_effort': model['reasoning_effort']}
    return len(json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def forecast():
    validate()
    registry = read(ORIGINAL / 'execution_models.json')['models']
    queries = selected_queries('screen')
    baseline = {f"{q['locale']}_{q['query_id']}": read(ORIGINAL / 'baseline/screen' / f"{q['locale']}_{q['query_id']}.json") for q in queries}
    records, source_hashes = {}, {}
    for model in registry:
        paths = sorted(p for p in (OUT / 'results/screen' / model_dir(model['id'])).glob('*.json') if not p.name.endswith('.started.json'))
        if len(paths) != 6:
            raise ValueError('Each model must have the same completed six-case pilot')
        records[model['id']] = [reconcile_accounting(read(p)) for p in paths]
        for p in paths:
            source_hashes[str(p.relative_to(ROOT))] = sha(p)
    with closing(sqlite3.connect(ROOT / 'evidence/api_budget.sqlite')) as db:
        task = db.execute("SELECT SUM(COALESCE(charged,reserved)) FROM calls WHERE purpose LIKE 'model-selection-100:%'").fetchone()[0] / 1e6
        pilot = db.execute("SELECT SUM(COALESCE(charged,reserved)) FROM calls WHERE purpose LIKE 'model-selection-100:screen:%'").fetchone()[0] / 1e6
    results = []
    for n in (12, 15, 18, 24):
        remaining = 0.0
        max_reserve = 0.0
        for model in registry:
            observations = records[model['id']]
            pin, pout = (float(model['pricing_usd_per_million'][k]) * 8 for k in ('input', 'output'))
            unknown_rate = sum(not r['response'].get('usage') for r in observations) / 6
            known = [r for r in observations if r['response'].get('usage')]
            for q in queries[6:n]:
                key = f"{q['locale']}_{q['query_id']}"
                size = encoded_size(model, baseline[key]['model_input'])
                native_bound = 262144 if model['id'] == 'kimi-k2.7-code' else 1024
                reserve = ((2 * size + 4096) * pin + (native_bound + 32) * pout) / 1e6 * 1.2
                max_reserve = max(max_reserve, reserve)
                same_locale = [r for r in known if r['locale'] == q['locale']] or known
                if same_locale:
                    ratios = [r['response']['usage']['prompt_tokens'] / encoded_size(model, baseline[f"{r['locale']}_{r['query_id']}"]['model_input']) for r in same_locale]
                    expected_tokens = size * mean(ratios)
                    cost = (expected_tokens * pin + mean(r['response']['usage']['completion_tokens'] for r in same_locale) * pout) / 1e6
                else:
                    cost = reserve
                remaining += unknown_rate * reserve + (1 - unknown_rate) * cost
        projected = task + remaining
        with_margin = task + remaining * 1.2
        results.append({'common_screen_queries_per_model': n, 'additional_screen_calls': (n - 6) * 100,
                        'projected_task_after_screen_cny': projected,
                        'projected_with_20pct_remaining_margin_cny': with_margin,
                        'largest_single_reservation_cny': max_reserve,
                        'screen_allocation_with_reservation_headroom_fits_82': with_margin + max_reserve <= 82})
    eligible = [r for r in results if r['screen_allocation_with_reservation_headroom_fits_82']]
    recommendation = max(eligible, key=lambda r: r['common_screen_queries_per_model']) if eligible else None
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'pilot_common_queries': 6, 'models': 100,
              'task_accounted_and_reserved_cny': task, 'pilot_accounted_and_reserved_cny': pilot,
              'calibration_and_prior_overhead_cny': task - pilot, 'projections': results,
              'recommended_common_screen_count': recommendation['common_screen_queries_per_model'] if recommendation else None,
              'selection_basis': 'Largest language-balanced count whose cost-only forecast plus 20% remaining-work margin and one maximum reservation fits the CNY 82 screening allocation.',
              'quality_scores_used': False, 'source_hashes': source_hashes,
              'limits': 'Six cases are a small cost sample; this is a planning estimate, not a confidence interval or a promise that all calls will fit. Actual atomic CNY 100 task cap still controls every request.'}
    save(OUT / 'pilot_cost_forecast.json', report)
    print(json.dumps({k: v for k, v in report.items() if k != 'source_hashes'}, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    forecast()
