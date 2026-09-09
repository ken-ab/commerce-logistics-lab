"""Transparent pre-test clarification alongside the unchanged v1 strict score.

This never overwrites a trajectory, original score, default-selection rule or
registered v1 method. It removes accidental dependence on one tool name and
accepts a complete actionable option without demanding every possible option.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from apparel_fulfillment.data import ROOT
from research.apparel_analysis import aggregate, paired
from research.apparel_evaluation import cited_field

DIRECTORY = ROOT / 'evidence/apparel_strategy_v1'


def nested(value, key, predicate=lambda value: bool(value)):
    if isinstance(value, dict):
        return (key in value and predicate(value[key])) or any(nested(v, key, predicate) for v in value.values())
    if isinstance(value, list): return any(nested(v, key, predicate) for v in value)
    return False


def task_audit(case, result):
    original = result['score']
    score = deepcopy(original)
    failures, gaps = set(score['failure_reasons']), set(score['evidence_gaps'])
    observations = [o for o in result['observations'].values() if o['success']]
    facts = result['report']['source_facts'] if result.get('report') else []
    changes = []
    # All of these are source-returning business tools. A checked order/proposal
    # already contains SKU, normalized attributes and explicit evidence IDs.
    observed_skus = set()
    def collect(value):
        if isinstance(value, dict):
            if value.get('sku') and (value.get('variant') or all(key in value for key in ('brand', 'size', 'color'))):
                observed_skus.add(value['sku'])
            for item in value.values(): collect(item)
        elif isinstance(value, list):
            for item in value: collect(item)
    for observation in observations: collect(observation['result'])
    chosen = {s['sku'] for s in result['after']['selections']}
    if 'selected_variant_source_not_read' in failures and chosen and chosen <= observed_skus:
        failures.remove('selected_variant_source_not_read')
        changes.append('Accept already-observed checked-order or variant source; a redundant read_variant call is not required.')
    if 'alternatives_not_observed' in failures and chosen and chosen <= observed_skus and not {'wrong_or_nonminimal_candidate', 'required_order_issue_missing'} & failures:
        failures.remove('alternatives_not_observed')
        changes.append('Accept another source-backed path to a verified minimum-difference candidate; find_alternatives is not the only valid tool sequence.')
    if 'substitution_differences_not_cited' in gaps and any(nested(f['value'], 'differences') for f in facts):
        gaps.remove('substitution_differences_not_cited')
        changes.append('A cited complete substitution object/list includes its differences.')
    if 'adjustment_options_not_cited' in gaps and result['after']['proposals']:
        latest = result['after']['proposals'][-1]
        ids = set()
        for observation in observations:
            value = observation['result'].get('proposal', observation['result'])
            if value.get('proposal_id') == latest['proposal_id']: ids.add(observation['observation_id'])
        current = [f for f in facts if f['observation_id'] in ids]
        for option in latest['route'].get('adjustment_options', []):
            # Every changed constraint of one real option must be present. Do not
            # combine budget from one route with deadline from an incompatible one.
            keys = [k for k in ('minimum_shipping_budget_cents', 'earliest_delivery_deadline_at') if k in option]
            full_option = any(f['value'] == option or nested(f['value'], 'adjustment_options', lambda choices: option in choices) for f in current)
            all_changes = keys and all(cited_field(current, key, option[key]) for key in keys)
            if full_option or all_changes:
                gaps.remove('adjustment_options_not_cited')
                changes.append('At least one complete feasible adjustment direction is cited; citing every alternative is unnecessary.')
                break
    if 'ready_order_status_not_cited' in gaps and case['family'] == 'shipping_revision' and result['before']['proposals']:
        old_id = result['before']['proposals'][-1]['proposal_id']
        old_obs = {o['observation_id'] for o in observations if o['tool'] == 'read_proposal' and o['result']['proposal']['proposal_id'] == old_id}
        if cited_field([f for f in facts if f['observation_id'] in old_obs], 'valid', case['expected']['old_valid']):
            gaps.remove('ready_order_status_not_cited')
            changes.append('For an unchanged, already-checked shipping order, a cited current validity assessment satisfies the requested status evidence.')
    score['failure_reasons'], score['evidence_gaps'] = sorted(failures), sorted(gaps)
    score['business_success'] = not failures and not score['constraint_violations']
    score['required_evidence_covered'] = not gaps
    score['task_completed'] = score['business_success'] and not gaps
    score['clarifications_applied'] = changes
    score['original_v1_task_completed'] = original['task_completed']
    return score


def register():
    from research.apparel_experiment import save, sha, validate
    validate()
    target = DIRECTORY / 'metric_clarification_v2.json'
    if target.exists(): raise FileExistsError('A clarification has already been registered')
    if (DIRECTORY / 'test/schedule.json').exists(): raise RuntimeError('Register only before any final model test begins')
    completed = list((DIRECTORY / 'validation').glob('AC-*/*/result.json'))
    save(target, {'registered_at': datetime.now(timezone.utc).isoformat(), 'timing': 'After inspecting partial validation, before any final test model calls',
         'validation_results_available': len(completed), 'original_method_sha256': sha(DIRECTORY / 'method.json'),
         'files_sha256': {name: sha(ROOT / name) for name in ('research/apparel_task_audit.py', 'research/APPAREL_METRIC_CLARIFICATION.md')},
         'policy': 'Supplementary semantic completion audit. Preserve and display original v1 strict scores, all failures and the original validation default-selection rule. No new or repeated model calls.'})
    print(json.dumps({'path': str(target), 'sha256': sha(target)}, ensure_ascii=False))


def analyze(partition):
    from research.apparel_experiment import save, sha, validate
    validate()
    registration = json.loads((DIRECTORY / 'metric_clarification_v2.json').read_text(encoding='utf-8'))
    for name, expected in registration['files_sha256'].items():
        if sha(ROOT / name) != expected: raise ValueError('Supplementary metric implementation changed')
    data = json.loads((ROOT / f'data/apparel_cases_{partition}_v1.json').read_text(encoding='utf-8'))
    by_arm, cases, inputs = defaultdict(list), [], {}
    for case in data['cases']:
        for arm in ('single', 'coordinator', 'on_demand'):
            path = DIRECTORY / partition / case['id'] / arm / 'result.json'
            raw = json.loads(path.read_text(encoding='utf-8'))
            assessed = task_audit(case, raw)
            cases.append({'case_id': case['id'], 'arm': arm, 'family': case['family'], 'score': assessed})
            inputs[str(path.relative_to(ROOT)).replace('\\', '/')] = sha(path)
            row = {k: v for k, v in raw.items() if k not in {'traces', 'before', 'after', 'report', 'observations'}}
            row['score'] = assessed
            by_arm[arm].append(row)
    summary = {'partition': partition, 'status': 'complete', 'metric_notice': registration['policy'],
               'registration_sha256': sha(DIRECTORY / 'metric_clarification_v2.json'), 'source_results_sha256': inputs,
               'arms': {arm: aggregate(rows) for arm, rows in by_arm.items()},
               'families': {name: {arm: aggregate([r for r in rows if r['family'] == name]) for arm, rows in by_arm.items()} for name in data['families']},
               'paired_differences': {arm + '_minus_single': paired(by_arm[arm], by_arm['single']) for arm in ('coordinator', 'on_demand')},
               'cases': cases}
    save(DIRECTORY / partition / 'task_audit_v2.json', summary)
    print(json.dumps({'partition': partition, 'completion': {a: s['task_accuracy'] for a, s in summary['arms'].items()}}, ensure_ascii=False))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=['register', 'validation', 'test'])
    args = parser.parse_args()
    register() if args.action == 'register' else analyze(args.action)
