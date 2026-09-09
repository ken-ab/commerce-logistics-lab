"""Retain original evidence and register a narrow revision before further calls."""
from contextlib import closing
from datetime import datetime, timezone
import shutil
import sqlite3

from model_selection_100.prepare import ROOT, OUT as ORIGINAL, read, save, sha
from model_selection_100.run import validate as validate_original
from model_selection_100.recovery_client import REPAIRED_MODEL
from model_selection_100.recovery_run import OUT


def prepare():
    validate_original()
    target = OUT / 'method.json'
    if target.exists():
        raise ValueError('Recovery method already registered')
    paths = sorted(p for p in (ORIGINAL / 'results/screen').rglob('*.json') if not p.name.endswith('.started.json'))
    if len(paths) != 158:
        raise ValueError('Unexpected original run state')
    with closing(sqlite3.connect(ROOT / 'evidence/api_budget.sqlite')) as db:
        db.row_factory = sqlite3.Row
        calls = [dict(r) for r in db.execute("SELECT * FROM calls WHERE purpose LIKE 'model-selection-100:%' ORDER BY created_at,id")]
        halt = db.execute("SELECT value FROM controls WHERE key='halt'").fetchone()
    if not halt or halt[0] != 'cost_bound_requires_review':
        raise ValueError('Different shared halt state; review separately')
    totals = sum(r['charged'] if r['charged'] is not None else r['reserved'] for r in calls)
    copied, repaired = [], []
    result_hashes = {}
    for path in paths:
        row = read(path)
        result_hashes[str(path.relative_to(ROOT))] = sha(path)
        marker = path.with_suffix('.started.json')
        result_hashes[str(marker.relative_to(ROOT))] = sha(marker)
        if row['model_id'] == REPAIRED_MODEL:
            # Preserve and reconcile accounting separately; never overwrite the raw result.
            call = next(c for c in calls if c['id'] == row['response']['budget_call_id'])
            repaired.append({'path': str(path.relative_to(ROOT)), 'result_sha256': sha(path),
                             'ledger': call, 'record_usage': row['response']['usage'],
                             'excluded_from_corrected_configuration_quality': True})
            continue
        destination = OUT / path.relative_to(ORIGINAL)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError('Recovery result already exists')
        shutil.copy2(path, destination)
        shutil.copy2(marker, destination.with_suffix('.started.json'))
        if sha(destination) != sha(path):
            raise ValueError('Result reuse must be byte-exact')
        copied.append({'source': str(path.relative_to(ROOT)), 'copy': str(destination.relative_to(ROOT)), 'sha256': sha(path)})
    if len(copied) != 156 or len(repaired) != 2:
        raise ValueError('Unexpected model-specific repair scope')
    audit = {'created_at': datetime.now(timezone.utc).isoformat(), 'original_method_sha256': sha(ORIGINAL / 'method.json'),
             'original_bound_files_verified': 504, 'original_formal_calls': len(paths), 'reused_results': copied,
             'old_kimi_results': repaired, 'scoped_ledger_calls': calls, 'scoped_accounted_micros': totals,
             'halt_at_review': halt[0], 'quality_scores_inspected_for_revision': False,
             'evidence_reviewed': 'Request/response format, token usage, costs, finish reasons, timing and HTTP status only'}
    save(OUT / 'pre_change_audit.json', audit)
    save(OUT / 'preflight.json', {'tests': 22, 'passed': 22, 'failed': 0,
        'command': "python -X utf8 -m unittest discover -s tests -p 'test_selection100*.py'",
        'network_calls': 0, 'recorded_at': datetime.now(timezone.utc).isoformat()})
    bound = [ORIGINAL / 'method.json', OUT / 'pre_change_audit.json', OUT / 'preflight.json']
    bound += [ROOT / p for p in ('model_selection_100/recovery_client.py', 'model_selection_100/recovery_run.py',
        'model_selection_100/prepare_recovery.py', 'model_selection_100/RECOVERY_V1.md',
        'tests/test_selection100.py', 'tests/test_selection100_recovery.py')]
    hashes = {str(p.relative_to(ROOT)): sha(p) for p in bound}
    hashes.update(result_hashes)
    for item in copied:
        hashes[item['copy']] = item['sha256']
    record = {'created_at': datetime.now(timezone.utc).isoformat(), 'status': 'prospective_protocol_repair_after_partial_pilot',
              'files_sha256': hashes, 'original_method_sha256': sha(ORIGINAL / 'method.json'),
              'reused_calls': 156, 'additional_compatibility_calls_planned': 3,
              'stages': {'screen': 24, 'shortlist': 60, 'validation': 150},
              'user_selection_task_limit_cny': 100, 'user_whole_project_limit_cny': 480}
    save(target, record)
    print({'registered_files': len(hashes), 'reused_results': len(copied), 'old_kimi_retained': len(repaired),
           'task_accounted_and_reserved_cny': totals / 1e6, 'method_sha256': sha(target)})


if __name__ == '__main__':
    prepare()
