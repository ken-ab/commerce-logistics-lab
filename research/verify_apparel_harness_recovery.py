"""Cross-check a saved recovery analysis without replaying any agent or tool."""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(study, analysis, cases):
    data = read(analysis)
    rows = {r['run']: r for r in data['rows']}
    assert len(rows) == len(data['rows']) == 144
    with cases.open(encoding='utf-8-sig', newline='') as file:
        table = list(csv.DictReader(file))
    assert len(table) == 144 and len({r['run'] for r in table}) == 144
    for row in table:
        assert set(row) == set(rows[row['run']])
        assert all(row[k] == ('' if v is None else str(v)) for k, v in rows[row['run']].items())
    for relative, expected in data['source_sha256_unchanged'].items():
        prefix = 'evidence/apparel_reliability_study_v1/'
        assert relative.startswith(prefix)
        assert sha(study / relative[len(prefix):]) == expected
    first_pass, raw_fee, rounded_fee, accounted = 0, Decimal(0), Decimal(0), Decimal(0)
    cross_checks = []
    call_count = 0
    for name, row in rows.items():
        execution = read(study / 'runs' / name / 'execution.json')
        root = [t for t in execution['traces'] if t['kind'] == 'model' and t['role'] == execution['arm']]
        attempts = [t for t in root if any(c.get('function', {}).get('name') == 'finish'
                    for c in t['response']['message'].get('tool_calls', []))]
        # Different representation from the analyzer's rejection-count partition:
        # compare the first root finish-producing message to the last root message.
        accepted_first = execution['report'] is not None and attempts[0]['call_number'] == root[-1]['call_number']
        assert accepted_first == row['root_first_finish_accepted']
        first_pass += accepted_first
        cross_checks.append({'run': name, 'first_finish_model_call': attempts[0]['call_number'],
                             'last_root_model_call': root[-1]['call_number'],
                             'first_finish_accepted_by_message_order': accepted_first})
        for call in execution['calls']:
            fee = Decimal(call['estimated_cost_cny'])
            raw_fee += fee
            rounded_fee += fee.quantize(Decimal('0.000001'), rounding=ROUND_CEILING)
            call_count += 1
        accounted += Decimal(execution['accounted_and_reserved_cny'])
    assert first_pass == 97 == data['totals']['root_first_finish_accepted']
    assert call_count == 745 == data['totals']['total_model_calls']
    assert raw_fee == Decimal(data['totals']['total_recorded_model_cost_cny']) == Decimal('12.1400160')
    assert rounded_fee == accounted == Decimal('12.140315')
    assert data['totals']['root_report_accepted_after_rejection'] == 46
    assert data['totals']['unfinished_after_root_rejection'] == 1
    assert data['totals']['original_task_passed'] == 143
    return {'checked_at': datetime.now(timezone.utc).isoformat(), 'passed': True,
            'analysis_sha256': sha(analysis), 'cases_sha256': sha(cases),
            'source_files_sha256_verified': len(data['source_sha256_unchanged']),
            'case_rows_verified': len(rows), 'first_finish_message_order_checks': cross_checks,
            'raw_per_call_estimate_cny': str(raw_fee), 'rounded_per_call_micro_cny_sum': str(rounded_fee),
            'accounted_and_reserved_cny': str(accounted), 'rounding_difference_cny': str(accounted - raw_fee),
            'interpretation': 'Per-call rounding explains the difference; neither total is a fresh provider invoice.',
            'new_model_calls': 0, 'new_gpu_calls': 0, 'new_business_runs': 0, 'new_paid_cny': 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--study', type=Path, required=True)
    parser.add_argument('--analysis', type=Path, required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Retain earlier verification; choose a fresh output path')
    result = verify(args.study, args.analysis, args.cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'first_finish_message_order_checks'}))


if __name__ == '__main__':
    main()
