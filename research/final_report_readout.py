"""Describe all frozen business and report outcomes without changing either score.

Added after v1 business results were visible, while the three report audits ran.
Combined counts and intervals are descriptive post-hoc analyses, not new gates.
"""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import csv
import hashlib
import io
import json
from pathlib import Path

from evaluation.business_metrics import paired

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unique(rows, field, expected):
    indexed = {row[field]: row for row in rows}
    if len(indexed) != len(rows) or set(indexed) != expected:
        raise ValueError(f'Missing, duplicate or unexpected {field} in complete readout')
    return indexed


def main():
    registration_path = ROOT / 'evidence/final_report_audit_registration.json'
    registration = read(registration_path)
    if registration['status'] != 'complete':
        raise SystemExit('All three registered audits must finish before producing a final readout.')
    business = Path(registration['business_directory'])
    cases = [c for c in read(ROOT / 'data/commerce_cases_v1.json')['cases'] if c['partition'] == 'test']
    expected = {c['id'] for c in cases}
    if len(cases) != 160 or len(expected) != 160:
        raise ValueError('Expected exactly 160 final cases')
    families = {c['id']: c['family'] for c in cases}
    arms, combined_rows, table = {}, {}, []
    for arm in registration['order']:
        saved = registration['arms'][arm]
        if saved['status'] != 'complete':
            raise ValueError(f'{arm} audit is incomplete')
        audit = Path(saved['directory'])
        config = read(audit / 'config.json')
        if (config['campaign'] != str(business / arm)
                or config['source_results_sha256'] != sha(business / arm / 'results.json')):
            raise ValueError(f'{arm} source identity mismatch')
        generated = unique(read(business / arm / 'results.json'), 'case_id', expected)
        audited = unique(read(audit / 'results.json'), 'id', expected)
        status_counts, verdict_counts, failure_combinations = Counter(), Counter(), Counter()
        combined_rows[arm] = []
        by_family = {f: {'scheduled': 0, 'business_passed': 0, 'judge_supported': 0,
                         'business_and_judge_supported': 0} for f in sorted(set(families.values()))}
        audit_settled = Decimal(0)
        for ident in sorted(expected):
            raw, checked = generated[ident], audited[ident]
            status = checked['status']
            verdict = checked.get('decision', {}).get('verdict')
            if status not in {'audited', 'audit_failed', 'not_assessable'}:
                raise ValueError('Unknown audit status')
            if status == 'audited' and verdict not in {'supported', 'unsupported', 'insufficient_evidence'}:
                raise ValueError('Unknown completed verdict')
            if status != 'audited' and verdict is not None:
                raise ValueError('An unsuccessful audit cannot supply a verdict')
            status_counts[status] += 1
            verdict_counts[verdict or status] += 1
            business_passed = bool(raw['score']['passed'])
            supported = status == 'audited' and verdict == 'supported'
            joint = business_passed and supported
            failure_combinations[f'business_{"passed" if business_passed else "failed"}__judge_{"supported" if supported else "not_supported_or_unknown"}'] += 1
            combined_rows[arm].append({'case_id': ident, 'score': {'passed': joint}})
            family = by_family[families[ident]]
            family['scheduled'] += 1
            family['business_passed'] += int(business_passed)
            family['judge_supported'] += int(supported)
            family['business_and_judge_supported'] += int(joint)
            audit_settled += Decimal(checked.get('response_metadata', {}).get('estimated_cost_cny', '0'))
            table.append({'arm': arm, 'case_id': ident, 'family': families[ident],
                          'business_passed': int(business_passed), 'audit_status': status,
                          'judge_verdict': verdict or '', 'business_and_judge_supported': int(joint)})
        total_supported = sum(f['judge_supported'] for f in by_family.values())
        joint_count = sum(f['business_and_judge_supported'] for f in by_family.values())
        arms[arm] = {'scheduled': len(expected), 'audit_status_counts': dict(status_counts),
                     'verdict_or_unavailable_counts': dict(verdict_counts),
                     'business_passed': sum(f['business_passed'] for f in by_family.values()),
                     'judge_supported': total_supported, 'judge_supported_rate_all_scheduled': total_supported / len(expected),
                     'judge_supported_rate_among_completed_audits': total_supported / status_counts['audited'] if status_counts['audited'] else None,
                     'business_and_judge_supported': joint_count,
                     'business_and_judge_supported_rate_all_scheduled': joint_count / len(expected),
                     'failure_combinations': dict(failure_combinations), 'by_family': by_family,
                     'audit_success_response_cost_estimate_cny': str(audit_settled),
                     'audit_cost_scope': 'Only valid completed judge responses represented here; schema failures, transport reservations and other research calls remain in global ledger.',
                     'sources': {'business_results': str(business / arm / 'results.json'),
                                 'business_results_sha256': sha(business / arm / 'results.json'),
                                 'audit_directory': str(audit), 'audit_results_sha256': sha(audit / 'results.json')}}
    output = {'created_at': datetime.now(timezone.utc).isoformat(), 'arms': arms,
              'source_registration_sha256': sha(registration_path),
              'scope': 'Descriptive post-hoc conjunction of unchanged tau-bench custom-domain scores and frozen Qwen v8 judgments. Judge support is not verified factual truth; no promotion decision is changed.',
              'conjunction_pairwise_descriptive': {
                  arm + '_vs_baseline_multi': paired(combined_rows['baseline_multi'], combined_rows[arm], cases)
                  for arm in ('candidate_multi', 'baseline_single')},
              'limitations': ['160 tasks per arm, 20 product groups sharing 8 templates; synthetic operations and zero real users.',
                              'No-report, transport and schema failures remain in every scheduled-case denominator.',
                              'Model flags require contextual review and can be false positives; companion analysis cannot overwrite the frozen judgment.',
                              'Combined endpoint was summarized after business outcomes were visible; intervals are descriptive and not a new selection criterion.']}
    destination = ROOT / 'evidence/final_report_quality.json'
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=list(table[0]))
    writer.writeheader()
    writer.writerows(table)
    (ROOT / 'evidence/final_report_quality_cases.csv').write_text(buffer.getvalue(), encoding='utf-8-sig', newline='')
    print(json.dumps({'file': str(destination), 'arms': {k: {f: v[f] for f in
        ('scheduled', 'business_passed', 'judge_supported', 'business_and_judge_supported', 'audit_status_counts')}
        for k, v in arms.items()}}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
