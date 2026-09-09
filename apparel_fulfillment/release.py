"""Read verified experiment evidence for the local workspace, independently of its business tools."""
from datetime import datetime, timezone
import hashlib
import json

from apparel_fulfillment.data import ROOT

STUDY = ROOT / 'evidence/apparel_strategy_v1'
RECEIPT = ROOT / 'evidence/apparel_release_v1.json'


def read(path): return json.loads(path.read_text(encoding='utf-8'))
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def certify():
    from research.apparel_experiment import validate, save
    validate()
    primary = read(STUDY / 'test/summary.json')
    supplement = read(STUDY / 'test/task_audit_v3.json')
    selection = read(STUDY / 'validation/selection.json')
    supplementary_method = read(STUDY / 'metric_clarification_v3_runtime_fix.json')
    for name, expected in supplementary_method['files_sha256'].items():
        if sha(ROOT / name) != expected: raise ValueError('Supplementary evaluator differs from its registered runtime fix')
    if primary['status'] != 'complete' or primary['runs'] != 324 or supplement['status'] != 'complete':
        raise ValueError('All final runs and both evidence readouts are required')
    inputs = supplement['source_results_sha256']
    if len(inputs) != 324: raise ValueError('Expected one raw result for each scheduled case/arm')
    for name, expected in inputs.items():
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT.resolve()) or sha(path) != expected:
            raise ValueError('A raw final result differs from the audited evidence')
    selected = selection['selected_arm']
    default_enabled = selected is not None and supplement['arms'][selected]['constraint_violation_runs'] == 0
    paths = ['evidence/apparel_strategy_v1/method.json', 'evidence/apparel_strategy_v1/validation/selection.json',
             'evidence/apparel_strategy_v1/test/summary.json', 'evidence/apparel_strategy_v1/test/task_audit_v3.json',
             'evidence/apparel_strategy_v1/metric_clarification_v2.json', 'evidence/apparel_strategy_v1/metric_clarification_v3_runtime_fix.json']
    value = {'certified_at': datetime.now(timezone.utc).isoformat(), 'status': 'verified_complete',
             'selected_by_validation': selected, 'default_enabled': default_enabled,
             'notice': 'Default strategy is fixed by validation, not reselected from test scores. Confirmation remains a separate operator action.',
             'files_sha256': {name: sha(ROOT / name) for name in paths} | supplementary_method['files_sha256'],
             'raw_results_manifest_sha256': hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()}
    if RECEIPT.exists(): raise FileExistsError('Preserve the existing release receipt')
    save(RECEIPT, value)
    return value


def snapshot():
    if RECEIPT.exists():
        receipt = read(RECEIPT)
        from research.apparel_experiment import validate
        validate()
        if any(sha(ROOT / name) != expected for name, expected in receipt['files_sha256'].items()):
            raise ValueError('The displayed research receipt no longer matches its evidence')
        primary, supplement = read(STUDY / 'test/summary.json'), read(STUDY / 'test/task_audit_v3.json')
        return {'status': 'complete', 'runs_completed': primary['runs'], 'runs_planned': 324,
                'cases_per_arm': primary['cases_per_arm'], 'selected_arm': receipt['selected_by_validation'],
                'default_enabled': receipt['default_enabled'], 'arms': supplement['arms'], 'strict_v1_arms': primary['arms'],
                'families': supplement['families'], 'paired_differences': supplement['paired_differences'],
                'metric_notice': '补充任务完成率与原始严格清单通过率同时保留；模拟环境结果，不是实际商家成功率。'}
    if (STUDY / 'test/schedule.json').exists():
        return {'status': 'test_running', 'runs_completed': len(list((STUDY / 'test').glob('AC-*/*/result.json'))), 'runs_planned': 324}
    if (STUDY / 'method.json').exists():
        return {'status': 'validation_running', 'runs_completed': len(list((STUDY / 'validation').glob('AC-*/*/result.json'))), 'runs_planned': 54}
    return {'status': 'development', 'runs_completed': 0, 'runs_planned': 378}


if __name__ == '__main__':
    print(json.dumps(certify(), ensure_ascii=False, indent=2))
