"""Describe root-report recovery in a completed, frozen 144-run study.

No model/provider, agent, live database, tool or scorer is invoked. The original
evaluation remains authoritative. Later calls are temporal follow-on work, not
a causal estimate of what disabling a gate would save.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def model_cost(calls):
    assert all(c['status'] == 'success' and c.get('estimated_cost_cny') is not None for c in calls)
    return sum((Decimal(c['estimated_cost_cny']) for c in calls), Decimal(0))


def summarize(rows):
    root_rejected = [r for r in rows if r['root_finish_rejections']]
    total_cost = sum((Decimal(r['total_recorded_model_cost_cny']) for r in rows), Decimal(0))
    following_cost = sum((Decimal(r['following_root_rejection_cost_cny']) for r in rows), Decimal(0))
    return {
        'runs': len(rows), 'original_task_passed': sum(r['original_task_passed'] for r in rows),
        'root_first_finish_accepted': sum(r['root_first_finish_accepted'] for r in rows),
        'runs_with_root_finish_rejection': len(root_rejected),
        'root_report_accepted_after_rejection': sum(r['root_report_accepted'] for r in root_rejected),
        'unfinished_after_root_rejection': sum(not r['root_report_accepted'] for r in root_rejected),
        'root_finish_requests': sum(r['root_finish_requests'] for r in rows),
        'root_finish_rejections': sum(r['root_finish_rejections'] for r in rows),
        'root_content_rejections': sum(r['root_content_rejections'] for r in rows),
        'root_control_rejections': sum(r['root_control_rejections'] for r in rows),
        'expert_finish_rejections': sum(r['expert_finish_rejections'] for r in rows),
        'runs_with_expert_finish_rejection': sum(r['expert_finish_rejections'] > 0 for r in rows),
        'root_text_only_model_responses': sum(r['root_text_only_model_responses'] for r in rows),
        'total_model_calls': sum(r['total_model_calls'] for r in rows),
        'following_root_rejection_model_calls': sum(r['following_root_rejection_model_calls'] for r in rows),
        'following_root_rejection_input_tokens': sum(r['following_root_rejection_input_tokens'] for r in rows),
        'following_root_rejection_output_tokens': sum(r['following_root_rejection_output_tokens'] for r in rows),
        'total_recorded_model_cost_cny': str(total_cost),
        'following_root_rejection_cost_cny': str(following_cost),
        'following_root_rejection_model_latency_seconds': round(sum(r['following_root_rejection_model_latency_seconds'] for r in rows), 6),
        'average_total_run_latency_seconds': round(sum(r['total_run_latency_seconds'] for r in rows) / len(rows), 6),
        'average_following_calls_per_root_rejected_run': (round(sum(r['following_root_rejection_model_calls'] for r in rows) / len(root_rejected), 6) if root_rejected else None),
        'average_following_cost_per_root_rejected_run_cny': str(following_cost / len(root_rejected)) if root_rejected else None,
    }


def analyze(study, reference):
    # The prior execution-layer receipt binds every original run and result.
    expected = read(reference)['source_sha256_unchanged']
    sources = {}
    for rel, fingerprint in expected.items():
        stem = 'evidence/apparel_reliability_study_v1/'
        assert rel.startswith(stem)
        p = study / rel[len(stem):]
        assert sha(p) == fingerprint, f'Frozen source changed: {rel}'
        sources[rel] = fingerprint
    files = sorted((study / 'runs').glob('*/execution.json'))
    assert len(files) == 144 and len(sources) == 289
    rows, details = [], []
    for p in files:
        execution, result = read(p), read(p.with_name('result.json'))
        assert sha(p) == result['execution_sha256']
        arm = result['arm']
        assert execution['arm'] == arm
        calls, traces = execution['calls'], execution['traces']
        model_traces = [t for t in traces if t['kind'] == 'model']
        assert [t['call_number'] for t in model_traces] == list(range(1, len(calls) + 1))
        assert len(calls) == execution['model_calls'] == result['model_calls']
        assert all(t['response']['budget_call_id'] == calls[t['call_number'] - 1]['budget_call_id'] for t in model_traces)
        root_models = [t for t in model_traces if t['role'] == arm]
        finish_counts = [sum(c.get('function', {}).get('name') == 'finish'
                             for c in t['response']['message'].get('tool_calls', [])) for t in root_models]
        # Multiple finish requests in one root message would need finer matching.
        assert all(n <= 1 for n in finish_counts), 'Ambiguous multi-finish message'
        finish_requests = sum(finish_counts)
        root_content = [t for t in traces if t['role'] == arm and t['kind'] == 'report_rejected']
        root_control = [t for t in traces if t['role'] == arm and t['kind'] == 'tool_rejected' and t.get('tool') == 'finish']
        expert_rejections = [t for t in traces if t['role'] != arm and
            (t['kind'] == 'report_rejected' or t['kind'] == 'tool_rejected' and t.get('tool') == 'finish')]
        accepted = execution['report'] is not None
        assert finish_requests >= 1
        assert finish_requests == len(root_content) + len(root_control) + int(accepted)
        assert accepted == (execution['run_status'] == 'completed')
        assert bool(result['evaluation']['passed']) == accepted  # Observed only in this dataset.
        seen, first = 0, None
        last_by_role = {}
        rejection_events = []
        for index, t in enumerate(traces):
            if t['kind'] == 'model':
                seen = t['call_number']
                last_by_role[t['role']] = t
            is_root_rejection = t['role'] == arm and (
                t['kind'] == 'report_rejected' or t['kind'] == 'tool_rejected' and t.get('tool') == 'finish')
            if not is_root_rejection:
                continue
            parent = last_by_role[arm]
            assert sum(c.get('function', {}).get('name') == 'finish' for c in parent['response']['message'].get('tool_calls', [])) == 1
            event = {'trace_index': index, 'kind': t['kind'], 'parent_model_call_number': parent['call_number'],
                     'all_model_calls_completed_at_rejection': seen,
                     'invalid_reasons': [e['reason'] for e in t.get('invalid', [])]}
            rejection_events.append(event)
            if first is None:
                first = event
        cutoff = first['all_model_calls_completed_at_rejection'] if first else len(calls)
        following = calls[cutoff:]
        total_cost = model_cost(calls)
        assert total_cost == Decimal(execution['settled_cost_cny'])
        row = {
            'run': p.parent.name, 'case_id': result['case_id'], 'family': result['family'],
            'version': result['version'], 'arm': arm, 'original_task_passed': bool(result['evaluation']['passed']),
            'run_status': execution['run_status'], 'root_report_accepted': accepted,
            'root_first_finish_accepted': accepted and not rejection_events,
            'root_finish_requests': finish_requests, 'root_finish_rejections': len(rejection_events),
            'root_content_rejections': len(root_content), 'root_control_rejections': len(root_control),
            'expert_finish_rejections': len(expert_rejections),
            'root_text_only_model_responses': sum(not t['response']['message'].get('tool_calls') for t in root_models),
            'first_root_rejection_after_model_call': cutoff if first else None,
            'total_model_calls': len(calls), 'following_root_rejection_model_calls': len(following),
            'total_recorded_model_cost_cny': str(total_cost),
            'following_root_rejection_cost_cny': str(model_cost(following)),
            'following_root_rejection_input_tokens': sum(c['usage']['prompt_tokens'] for c in following),
            'following_root_rejection_output_tokens': sum(c['usage']['completion_tokens'] for c in following),
            'following_root_rejection_model_latency_seconds': round(sum(c['latency_seconds'] for c in following), 6),
            'total_run_latency_seconds': execution['latency_seconds'],
        }
        rows.append(row)
        if first:
            details.append({'run': p.parent.name, 'root_rejections': rejection_events,
                            'first_rejection': first, 'run_status': execution['run_status'],
                            'following_model_call_numbers': list(range(cutoff + 1, len(calls) + 1))})
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['version'] + '_' + row['arm']].append(row)
    assert len(grouped) == 6 and all(len(v) == 24 for v in grouped.values())
    totals = summarize(rows)
    assert (totals['runs'], totals['original_task_passed'], totals['total_model_calls']) == (144, 143, 745)
    assert (totals['root_finish_requests'], totals['root_finish_rejections']) == (203, 60)
    return {'created_at': datetime.now(timezone.utc).isoformat(), 'analysis': 'retrospective-root-finish-recovery-v1',
            'scope': 'Same completed 24 simulated states x 2 versions x 3 strategies; not 144 new cases or users.',
            'prior_execution_layer_manifest_sha256': sha(reference), 'source_sha256_unchanged': sources,
            'definitions': {
                'root': 'The single executor, coordinator, or on-demand root; expert subtask finish requests are counted separately.',
                'root_first_finish_accepted': 'An accepted final report with no earlier root finish rejection. Every run has >=1 root finish request and no model message contains multiple root finishes in these data.',
                'root_content_rejection': 'report_rejected trace from citation or operation/source checks.',
                'root_control_rejection': 'tool_rejected trace for root finish, including schema and routing checks.',
                'following_work': 'All model calls initiated after the first root finish rejection, excluding the call that produced it. May include necessary business work and experts; not wholly attributable to the gate.',
                'cost': 'Sum of original per-call estimated_cost_cny; no repricing. This is not provider billing or account balance.',
                'latency': 'Following-call latency is the sum of recorded model request latencies, not full wall-clock recovery time or hidden reasoning duration.',
                'task_passed': 'Original evaluation.passed, never rescored; it happens to match accepted-root-report status in this dataset.',
            },
            'totals': totals, 'groups': {k: summarize(v) for k, v in sorted(grouped.items())},
            'rows': rows, 'root_rejection_details': details,
            'limitations': ['No gate-disabled control; no causal claim that a gate caused these recoveries or that later costs disappear without it.',
                            'No free-text rationale correctness guarantee. Acceptance can correctly mean clarification or infeasibility.',
                            'Same developer-designed simulated states and saved first attempts; no new independent sample, live customer, transaction or shipment.',
                            'Version changes bundle interventions; this is not a per-hook ablation or a new model comparison.'],
            'new_model_calls': 0, 'new_gpu_calls': 0, 'new_business_runs': 0, 'new_paid_cny': 0, 'real_users_added': 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--study', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Choose a new output directory; retain earlier analyses')
    result = analyze(args.study, args.reference)
    args.output.mkdir(parents=True)
    (args.output / 'analysis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with (args.output / 'cases.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(result['rows'][0]))
        writer.writeheader()
        writer.writerows(result['rows'])
    print(json.dumps({'output': str(args.output), 'totals': result['totals'], 'groups': result['groups']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
