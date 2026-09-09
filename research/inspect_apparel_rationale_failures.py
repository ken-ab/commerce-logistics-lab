"""Offline inventory of existing audit failures; never repairs or resamples verdicts."""
import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Use a new output path; preserve previous receipts')
    sources = {}

    def tracked(name):
        path = ROOT / name
        sources[str(path.relative_to(ROOT)).replace('\\', '/')] = sha(path)
        return json.loads(path.read_text(encoding='utf-8-sig'))

    judge_path = ROOT / 'evaluation/report_judge.py'
    sources['evaluation/report_judge.py'] = sha(judge_path)
    tree = ast.parse(judge_path.read_text(encoding='utf-8'))
    names = {'SegmentClaim', 'ClaimAudit', 'segment_report'}
    nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    assert {node.name for node in nodes} == names
    scope = dict(BaseModel=BaseModel, ConfigDict=ConfigDict, Field=Field, Literal=Literal, re=re)
    # Load only the original pure schema and segmenter, without provider imports.
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(judge_path), 'exec'), scope)
    audit_schema, segment_report = scope['ClaimAudit'], scope['segment_report']

    directory = Path('evidence/apparel_rationale_account_recovery_v1')
    summary = tracked(directory / 'summary.json')
    rows = tracked(directory / 'combined_results.json')
    assert sha(ROOT / directory / 'combined_results.json') == summary['combined_results_sha256']
    assert len(rows) == 144
    failures = [row for row in rows if row['status'] == 'audit_failed']
    assert len(failures) == 38
    inventory, lengths, types = [], [], Counter()

    for row in failures:
        ident = row['id']
        origin = summary['result_origins'][ident]
        original = tracked(origin['path'])
        assert sha(ROOT / origin['path']) == origin['sha256']
        assert original['error'] == row['error'] and original['status'] == row['status']
        raw_name = Path(origin['path']).parent.parent / 'raw' / f'{ident}.json'
        error = row.get('error', '')
        lower = error.lower()
        if '[Errno 2]' in error and 'account_recovery_v1' in error:
            category = 'raw_output_save_failure'
        elif row.get('transport_failures') or ('transport' in lower and 'allowance' in lower and 'exhausted' in lower):
            category = 'transport_failure_or_attempt_allowance_exhausted'
        else:
            category = 'schema_failure'
        item = dict(id=ident, original_status=row['status'], category=category,
                    raw_available=(ROOT / raw_name).is_file())
        if item['raw_available']:
            raw = tracked(raw_name)
            calls = raw['message'].get('tool_calls', [])
            assert raw['finish_reason'] in ('stop', 'tool_calls')
            assert len(calls) == 1 and calls[0]['function']['name'] == 'submit_audit'
            argument_text = calls[0]['function']['arguments']
            payload = json.loads(argument_text)
            try:
                audit_schema.model_validate_json(argument_text)
            except ValidationError as exc:
                errors = exc.errors(include_input=False, include_url=False)
            else:
                errors = []
            item['strict_schema_passed'] = not errors
            item['validation_errors'] = [{'location': list(e['loc']), 'type': e['type']} for e in errors]
            types.update(e['type'] for e in errors)
            item['explanation_character_lengths'] = [len(c['explanation']) for c in payload['claims']]
            lengths.extend(item['explanation_character_lengths'])
            execution = tracked(f'evidence/apparel_reliability_study_v1/runs/{ident}/execution.json')
            answer = execution['report']['decision']['rationale']
            expected_ids = {s['id'] for s in segment_report(answer)}
            observed_ids = [c['segment_id'] for c in payload['claims']]
            evidence_ids = {'TASK', 'CONTEXT', 'HOST_BEFORE', 'HOST_AFTER', 'LOG'} | set(execution['observations'])
            item['structural_checks_only'] = {
                'exact_segment_coverage': len(observed_ids) == len(expected_ids) and set(observed_ids) == expected_ids,
                'existing_evidence_ids': all(e in evidence_ids for c in payload['claims'] for e in c['evidence_ids']),
                'supported_claims_have_references': all(c['evidence_ids'] for c in payload['claims'] if c['status'] == 'supported'),
            }
            assert category == 'schema_failure'
        inventory.append(item)

    counts = dict(Counter(i['category'] for i in inventory))
    assert counts == {'schema_failure': 25, 'transport_failure_or_attempt_allowance_exhausted': 7, 'raw_output_save_failure': 6}
    assert all(not i.get('strict_schema_passed', False) for i in inventory)
    assert set(types) == {'string_too_long'}
    assert all(sha(ROOT / name) == value for name, value in sources.items())
    result = {
        'recorded_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'Offline structural inspection of the same 38 failed audits among 144 existing runs. No semantic rejudgment.',
        'original_totals_unchanged': {'runs': 144, 'audited': 105, 'audit_failed': 38, 'not_assessable': 1},
        'original_report_failure_split': {'schema_or_audit_failure': 26, 'transport_failure': 6, 'raw_output_save_failure': 6},
        'corrected_failure_split': counts,
        'classification_correction': {'id': 'RL-01-1-v6_coordinator', 'reason': 'Registered total transport-attempt allowance exhausted was missed by the old substring classifier.'},
        'saved_raw_outputs': sum(i['raw_available'] for i in inventory),
        'strict_schema_passes_among_saved_failures': 0,
        'schema_validation_error_counts': dict(types),
        'explanation_character_range': [min(lengths), max(lengths)],
        'structural_check_failures': {name: [i['id'] for i in inventory if name in i.get('structural_checks_only', {}) and not i['structural_checks_only'][name]] for name in ('exact_segment_coverage', 'existing_evidence_ids', 'supported_claims_have_references')},
        'rows': inventory,
        'source_sha256': sources,
        'script_sha256': sha(Path(__file__)),
        'original_files_unchanged': True,
        'new_model_calls': 0,
        'new_business_runs': 0,
        'new_paid_cny': 0,
        'limitations': 'No trimming, relaxed limits, new labels, restored lost responses or accuracy increase. Syntactic reference existence does not establish semantic support.',
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({k: result[k] for k in ('corrected_failure_split', 'saved_raw_outputs', 'schema_validation_error_counts', 'explanation_character_range', 'structural_check_failures', 'new_paid_cny')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
