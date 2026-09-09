"""Frozen-before-validation paired analysis; does not submit API requests."""
from contextlib import closing
from datetime import datetime, timezone
import argparse
import math
import random
import sqlite3

from model_selection_100.prepare import ROOT, OUT as ORIGINAL, read, save, sha
from model_selection_100 import budget100_run as run
from model_selection_100.calibrate import model_dir
from model_selection_100.configured import decode
from model_selection_100.metrics import quantile, score_order, summarize

OUT = run.OUT
SEED = 20260908
RESAMPLES = 10000
KEYS = ('ndcg_at_10', 'hit_exact_at_1', 'mrr_exact')


def paired_interval(candidate, reference, *, seed=SEED, resamples=RESAMPLES):
    if not candidate or len(candidate) != len(reference):
        raise ValueError('Paired arrays must have the same nonzero length')
    if resamples < 100 or any(not math.isfinite(v) for v in candidate + reference):
        raise ValueError('Invalid paired values or resample count')
    delta = [a-b for a, b in zip(candidate, reference)]
    rng = random.Random(seed)
    means = [sum(rng.choices(delta, k=len(delta)))/len(delta) for _ in range(resamples)]
    return {'queries': len(delta), 'mean_delta': sum(delta)/len(delta),
            'lower_95': quantile(means, .025), 'upper_95': quantile(means, .975),
            'resamples': resamples, 'seed': seed,
            'method': 'Unstratified paired query-group percentile bootstrap'}


def replacement_gate(candidate, reference, contrasts, *, same_model=False):
    complete = candidate.get('complete', False) and reference.get('complete', False)
    if not complete:
        return {'replace_current_model': False, 'decision': 'retain_current_model_incomplete_validation',
                'checks': {'both_complete': False}}
    if same_model:
        return {'replace_current_model': False, 'decision': 'retain_current_luna_model',
                'checks': {'both_complete': True},
                'note': 'Luna won the shortlist. Its new JSON protocol was not compared with the old tool protocol.'}
    ndcg = contrasts['ndcg_at_10']; accuracy = contrasts['hit_exact_at_1']
    more_expensive = candidate['avg_cost_cny'] > reference['avg_cost_cny']
    checks = {
        'both_complete': complete,
        'at_least_147_valid': candidate['valid_responses'] >= 147,
        'identity_consistent': candidate['identity_match_rate'] == 1,
        'ndcg_noninferiority': ndcg['lower_95'] >= -.02,
        'accuracy_noninferiority': accuracy['lower_95'] >= -.03,
        'overall_score_higher': candidate.get('overall_score') is not None
            and reference.get('overall_score') is not None
            and candidate['overall_score'] > reference['overall_score'],
        'premium_requires_ndcg_superiority': not more_expensive or ndcg['lower_95'] > 0,
    }
    passed = all(checks.values())
    return {'replace_current_model': passed,
            'decision': 'adopt_preselected_candidate' if passed else 'retain_current_luna_model',
            'candidate_more_expensive': more_expensive, 'checks': checks}


def validate_registration():
    run.validate()
    registration = read(OUT/'validation_analysis_registration.json')
    if registration['study_method_sha256'] != sha(OUT/'method.json'):
        raise ValueError('Study method changed')
    for path, digest in registration['files_sha256'].items():
        if sha(ROOT/path) != digest:
            raise ValueError('Registered validation analysis changed: '+path)
    return registration


def register():
    target = OUT/'validation_analysis_registration.json'
    if target.exists():
        return validate_registration()
    run.validate()
    if list((OUT/'results/validation').rglob('*.json')):
        raise ValueError('Validation has already started; cannot preregister analysis now')
    with closing(sqlite3.connect(ROOT/'evidence/api_budget.sqlite')) as db:
        n = db.execute("SELECT count(*) FROM calls WHERE purpose LIKE 'model-selection-100:validation:%'").fetchone()[0]
    if n:
        raise ValueError('Validation calls already exist in the ledger')
    paths = ['model_selection_100/validation_readout.py',
             'model_selection_100/VALIDATION_ANALYSIS.md',
             'tests/test_selection100_validation.py']
    result = {'created_at': datetime.now(timezone.utc).isoformat(),
              'status': 'registered_before_any_validation_submission',
              'study_method_sha256': sha(OUT/'method.json'),
              'files_sha256': {p: sha(ROOT/p) for p in paths},
              'bootstrap_resamples': RESAMPLES, 'seed': SEED,
              'selection_or_acceptance_thresholds_changed': False}
    save(target, result)
    return result


def audit_rows(stage, model, queries):
    expected = {f"{q['locale']}_{q['query_id']}": q for q in queries}
    paths = [p for p in (OUT/'results'/stage/model_dir(model['id'])).glob('*.json')
             if not p.name.endswith('.started.json')]
    rows = {}; hashes = {}; ledger_records = []; call_ids = set()
    with closing(sqlite3.connect(ROOT/'evidence/api_budget.sqlite')) as db:
        for path in paths:
            raw = read(path); key = f"{raw['locale']}_{raw['query_id']}"
            if key not in expected or key in rows or path.stem != key:
                raise ValueError('Unknown or repeated query group')
            if (raw['stage'] != stage or raw['model_id'] != model['id']
                or raw['query_group_sha256'] != expected[key]['query_group_sha256']
                or raw.get('submitted') is not True):
                raise ValueError('Result membership is inconsistent')
            baseline = read(ORIGINAL/'baseline'/stage/(key+'.json'))
            if raw.get('valid_response'):
                parsed = decode(model, raw['response'], [c['id'] for c in baseline['model_input']['candidates']])
                if parsed != raw['order']:
                    raise ValueError('Raw answer differs from the stored order')
                measured = score_order(baseline, parsed)
                if any(not math.isclose(raw['metrics'][k], measured[k], abs_tol=1e-12) for k in KEYS):
                    raise ValueError('Stored ranking metrics do not reproduce')
            response = raw['response']; call_id = response['budget_call_id']
            if call_id in call_ids:
                raise ValueError('Repeated paid request identifier')
            call_ids.add(call_id)
            ledger = db.execute('SELECT id,purpose,model,reserved,charged,status FROM calls WHERE id=?', (call_id,)).fetchone()
            purpose = f"model-selection-100:{stage}:{model['id']}:{key}"
            if not ledger or ledger[1] != purpose or ledger[2] != model['id']:
                raise ValueError('Result is not bound to its recorded request')
            ledger_records.append(dict(zip(['id','purpose','model','reserved_micro_cny','charged_micro_cny','status'], ledger)))
            rows[key] = run.reconcile_accounting(raw)
            hashes[str(path.relative_to(ROOT))] = sha(path)
    ordered = [rows[k] for k in expected if k in rows]
    return ordered, summarize(ordered, len(queries)), hashes, ledger_records


def readout():
    validate_registration()
    winner_file = OUT/'winner_selection.json'; selection = read(winner_file)
    if selection['source_summary_sha256'] != sha(OUT/'shortlist_summary.json'):
        raise ValueError('Frozen winner support changed')
    models = run.stage_models('validation'); queries = run.selected_queries('validation')
    winner = selection['winner']; reference = run.REFERENCE
    if set(m['id'] for m in models) != {winner, reference}:
        raise ValueError('Validation models differ from the single preselected comparison')
    summaries = {}; groups = {}; hashes = {}; ledger = []
    for model in models:
        rows, summary, files, calls = audit_rows('validation', model, queries)
        summaries[model['id']] = summary; groups[model['id']] = rows
        hashes.update(files); ledger.extend(calls)
    complete = all(s.get('complete') for s in summaries.values())
    base_rows = [read(ORIGINAL/'baseline/validation'/f"{q['locale']}_{q['query_id']}.json") for q in queries]
    base_values = {k: [score_order(b)[k] for b in base_rows] for k in KEYS}
    measured = {mid: {k: [r['metrics'][k] if r.get('valid_response') else 0 for r in rows]
                      for k in KEYS} for mid, rows in groups.items()}
    contrasts = {}; against_baseline = {}
    if complete:
        contrasts = {k: paired_interval(measured[winner][k], measured[reference][k]) for k in KEYS}
        against_baseline = {mid: {k: paired_interval(values[k], base_values[k]) for k in KEYS}
                            for mid, values in measured.items()}
    gate = replacement_gate(summaries[winner], summaries[reference], contrasts, same_model=winner == reference)
    result = {'created_at': datetime.now(timezone.utc).isoformat(), 'complete': complete,
              'study_method_sha256': sha(OUT/'method.json'),
              'analysis_registration_sha256': sha(OUT/'validation_analysis_registration.json'),
              'winner_selection_sha256': sha(winner_file), 'winner': winner, 'reference': reference,
              'summaries': summaries, 'paired_candidate_minus_reference': contrasts,
              'paired_models_minus_local_baseline': against_baseline,
              'local_baseline': {k: sum(v)/len(v) for k, v in base_values.items()},
              'gate': gate, 'raw_result_sha256': hashes, 'ledger_snapshot': ledger,
              'interpretation': 'Fixed labelled candidate pools; failures count as zero model quality. This is not full-catalogue recall or real-customer conversion.'}
    target = OUT/'validation_readout.json'
    if target.exists():
        raise ValueError('Validation readout already exists; preserve the original analysis')
    save(target, result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=['register','readout'])
    args = parser.parse_args()
    result = register() if args.action == 'register' else readout()
    print({'action': args.action, 'status': result.get('status'), 'gate': result.get('gate')})
