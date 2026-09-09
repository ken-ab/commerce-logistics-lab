"""One-time offline external replay of the frozen 144-run reliability study."""
from collections import Counter
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import time
from unittest.mock import patch
import xml.etree.ElementTree as ET

from loguru import logger
from evaluation.apparel_tau_bridge import VERSION, evaluate_record, read
from tau2.environment.environment import Environment
from tau2.evaluator.evaluator_env import EnvironmentEvaluator

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evidence/apparel_reliability_study_v1'
OUT = ROOT / 'evidence/apparel_tau_replay_v1'
PREVIOUS = ROOT / 'knowledge/reading_review_20260909_context.json'
COMMIT = '672227c6b6676edc20d57ea53b7000262aae77b9'
UPSTREAM_FILES = ['src/tau2/evaluator/evaluator_env.py', 'src/tau2/environment/environment.py',
    'src/tau2/environment/toolkit.py', 'src/tau2/environment/tool.py',
    'src/tau2/data_model/tasks.py', 'src/tau2/data_model/message.py']
NEW_SOURCE_FILES = ['evaluation/apparel_tau_bridge.py', 'tests/test_apparel_tau_bridge.py',
    'research/apparel_tau_replay.py', 'research/APPAREL_TAU_REPLAY_PROTOCOL.md',
    'evidence/apparel_tau_bridge_tests_20260909.xml', 'evidence/apparel_tau_bridge_final_tests_20260909.xml']


def sha(path):
    with Path(path).open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def save(path, value):
    with Path(path).open('x', encoding='utf-8') as file:
        file.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def ledger():
    with closing(sqlite3.connect((ROOT / 'evidence/api_budget.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        total, rows = db.execute('SELECT SUM(COALESCE(charged,reserved)),COUNT(*) FROM calls').fetchone()
    return {'micro_cny': total, 'rows': rows}


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT / 'upstream/tau2-bench'), *args], text=True).strip()


def preflight():
    previous = read(PREVIOUS)
    assert len(previous['knowledge_file_sha256']) == 183
    audit = read(ROOT / 'evidence/apparel_reliability_audit_20260909.json')
    verified = {}
    maps = [previous['knowledge_file_sha256'], previous['frozen_sha256'],
            audit['verified_sha256'], audit['post_audit_snapshot_sha256']]
    for mapping in maps:
        for name, value in mapping.items():
            assert sha(ROOT / name) == value, 'Frozen source changed: ' + name
            if name in verified:
                assert verified[name] == value
            verified[name] = value
    pdf = ROOT.parents[1] / 'artifacts/commerce_logistics_integrated_20260908/01_服装订单与跨境履约Agent_完整实测报告.pdf'
    assert sha(pdf) == previous['report_snapshot_sha256']
    assert git('rev-parse', 'HEAD') == COMMIT and git('status', '--porcelain') == ''
    package = ROOT / 'upstream/tau2-bench'
    assert Path(inspect.getfile(EnvironmentEvaluator)).resolve() == package / UPSTREAM_FILES[0]
    assert Path(inspect.getfile(Environment)).resolve() == package / UPSTREAM_FILES[1]
    for name in UPSTREAM_FILES:
        verified['upstream/tau2-bench/' + name] = sha(package / name)
    for name in NEW_SOURCE_FILES:
        verified[name] = sha(ROOT / name)
    tree = ET.parse(ROOT / 'evidence/apparel_tau_bridge_final_tests_20260909.xml')
    suites = tree.findall('testsuite')
    assert sum(int(s.attrib['tests']) for s in suites) == 13
    assert all(int(s.attrib['failures']) == int(s.attrib['errors']) == int(s.attrib['skipped']) == 0 for s in suites)
    return previous, verified


def main():
    if OUT.exists():
        raise FileExistsError('Preserve the completed or partial replay; do not overwrite it')
    previous, verified = preflight()
    registration = read(DATA / 'registration.json')
    study = read(DATA / 'summary.json')
    cases = {c['id']: c for c in read(DATA / 'cases.json')}
    assert len(cases) == 24 and len(set(map(tuple, registration['jobs']))) == len(registration['jobs']) == 144
    assert sha(DATA / 'registration.json') == study['registration_sha256']
    before = ledger()
    assert before == {'micro_cny': 339751400, 'rows': 18928}
    OUT.mkdir()
    save(OUT / 'registration.json', {'registered_at': datetime.now(timezone.utc).isoformat(),
        'adapter_version': VERSION, 'upstream_commit': COMMIT, 'unmodified_upstream': True,
        'source_registration_sha256': study['registration_sha256'], 'runs': 144,
        'cases': 24, 'conditions': registration['conditions'], 'jobs': registration['jobs'],
        'source_sha256': verified, 'prior_receipt_sha256': sha(PREVIOUS),
        'historical_pdf_sha256': previous['report_snapshot_sha256'], 'ledger_before': before,
        'scope': 'Post-hoc replay of existing tool executions; not new model trials or official tau2 benchmark tasks.',
        'reference_basis': 'Frozen case expected.proposal labels and initial state, never model output.',
        'checks': ['upstream_strict_tool_replay', 'native_json_output_types', 'all_persistent_business_state', 'reference_target_database'],
        'tests': {'passed': 13, 'initial_development_run': '12 passed, 1 failed',
            'correction': 'Test initially assumed upstream preserved numeric JSON types. Inspection showed primitive-to-string serialization; changed the counterexample from float to numeric string. No production or upstream code was modified.'}})
    logger.remove()
    logger.add(OUT / 'upstream.log', level='WARNING', encoding='utf-8')
    records, blocked = [], []

    def reject_connection(*args, **kwargs):
        blocked.append('socket_connection_attempt')
        raise AssertionError('Network is disabled during offline replay')

    started = time.monotonic()
    with ExitStack() as guard:
        guard.enter_context(patch.object(socket.socket, 'connect', reject_connection))
        guard.enter_context(patch.object(socket.socket, 'connect_ex', reject_connection))
        guard.enter_context(patch.object(socket, 'create_connection', reject_connection))
        for ident, condition in registration['jobs']:
            folder = DATA / 'runs' / (ident + '-' + condition)
            execution, original = read(folder / 'execution.json'), read(folder / 'result.json')
            assert sha(folder / 'execution.json') == original['execution_sha256']
            version, arm = condition.split('_', 1)
            assert execution['arm'] == arm
            result = evaluate_record(cases[ident], execution, DATA / 'initial' / ident,
                folder / 'operations.sqlite', OUT / 'runs' / (ident + '-' + condition), version)
            records.append({**result, 'condition': condition,
                'original_strict_task_passed': original['evaluation']['passed'],
                'original_failures': original['evaluation']['failures'],
                'original_run_status': original['run_status'],
                'original_execution_sha256': original['execution_sha256']})
            if len(records) % 12 == 0:
                print(json.dumps({'replayed': len(records), 'total': 144,
                    'fidelity_passed': sum(r['replay_fidelity_passed'] for r in records),
                    'target_db_match': sum(r['target_db_match'] for r in records),
                    'elapsed_seconds': round(time.monotonic() - started, 2), 'new_model_calls': 0}), flush=True)
    after = ledger()
    for name, value in verified.items():
        assert sha(ROOT / name) == value, 'Input changed during replay: ' + name
    assert git('status', '--porcelain') == ''
    groups = {}
    for condition in registration['conditions']:
        chosen = [r for r in records if r['condition'] == condition]
        groups[condition] = {'runs': len(chosen), **{k: sum(r[k] for r in chosen) for k in
            ('replay_fidelity_passed', 'strict_replay_passed', 'exact_output_types_passed', 'live_state_match',
             'reference_actions_succeeded', 'target_db_match', 'recorded_tools', 'replayed_tools',
             'recorded_business_errors', 'original_strict_task_passed')}}
        assert groups[condition]['runs'] == study['groups'][condition]['runs']
        assert groups[condition]['original_strict_task_passed'] == study['groups'][condition]['passed']
        assert groups[condition]['recorded_tools'] == study['groups'][condition]['tool_calls']
    files = {p.relative_to(OUT).as_posix(): sha(p) for p in sorted((OUT / 'runs').rglob('*')) if p.is_file()}
    zero_cost = before == after and not blocked
    output = {'completed_at': datetime.now(timezone.utc).isoformat(), 'adapter_version': VERSION,
        'upstream_commit': COMMIT, 'unmodified_upstream': True,
        'registration_sha256': sha(OUT / 'registration.json'), 'groups': groups, 'records': records,
        'offline_elapsed_seconds': round(time.monotonic() - started, 6),
        'all_replay_checks_passed': all(r['replay_fidelity_passed'] and r['target_db_match'] for r in records),
        'network_connection_attempts': len(blocked), 'no_new_paid_rows_verified': zero_cost,
        'ledger_before': before, 'ledger_after': after, 'new_model_calls': 0, 'new_gpu_calls': 0,
        'new_model_cost_cny': (after['micro_cny'] - before['micro_cny']) / 1000000,
        'new_generative_agent_runs': 0, 'new_real_users': 0, 'new_business_cases': 0,
        'input_hashes_verified_before_and_after': len(verified), 'artifact_sha256': files,
        'limitations': ['Custom developer-authored apparel cases; no official tau2 score or certification.',
            'Tool return payloads and seven persistent business tables are replayed; model prose, routing decisions and completion logic are not rerun.',
            'No general claims for unseen merchants or repeated stochastic trajectories.',
            'Reference route equality uses the same frozen deterministic planner; independent feasibility evidence remains in the original audit.',
            'Only generated proposal UUIDs are injected for exact replay; all business fields are recomputed.',
            'The original strict task failure remains a failure even when its final database is correct.',
            'This snapshot contains no candidate searches, confirmation transactions or new model/GPU execution.']}
    save(OUT / 'summary.json', output)
    print(json.dumps({k: v for k, v in output.items() if k not in ('records', 'artifact_sha256')}, ensure_ascii=False, indent=2))
    if not zero_cost or not output['all_replay_checks_passed']:
        raise SystemExit('Replay has retained failures; inspect summary before interpreting results')


if __name__ == '__main__':
    main()
