"""Read-only reconciliation of replay DB traces, snapshots, criteria and hashes.

No imports from the adapter, Agent, planner, tau2 or model clients.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evidence/apparel_reliability_study_v1'
REPLAY = ROOT / 'evidence/apparel_tau_replay_v1'
OUT = ROOT / 'evidence/apparel_tau_replay_audit_20260909.json'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def text(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(text(value).encode()).hexdigest()


def connect(path):
    assert not Path(str(path) + '-wal').exists() or Path(str(path) + '-wal').stat().st_size == 0
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)


def state(path):
    result = {}
    with closing(connect(path)) as db:
        db.row_factory = sqlite3.Row
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        assert set(tables) == {'metadata','inventory','drafts','approvals','proposals','transport_events','confirmations','traces'}
        for table in sorted(set(tables) - {'traces'}):
            rows = [dict(r) for r in db.execute('SELECT * FROM ' + table)]
            for row in rows:
                for column in ('payload','request','selections'):
                    if column in row:
                        row[column] = json.loads(row[column])
            result[table] = sorted(rows, key=text)
    return result


def reference_equivalent(predicted, reference):
    """Bind each reference row to the predicted row with the same owner/version."""
    predicted_ids = {(p['draft_id'], p['version']): p['id'] for p in predicted['proposals']}
    reference_ids = {(p['draft_id'], p['version']): p['id'] for p in reference['proposals']}
    assert len(predicted_ids) == len(predicted['proposals'])
    assert predicted_ids.keys() == reference_ids.keys()
    mapping = {ident: predicted_ids[key] for key, ident in reference_ids.items()}

    def bind(value):
        if isinstance(value, str): return mapping.get(value, value)
        if isinstance(value, dict): return {k: bind(v) for k, v in value.items()}
        if isinstance(value, list): return [bind(v) for v in value]
        return value

    bound = {table: sorted((bind(r) for r in rows), key=text) for table, rows in reference.items()}
    return text(bound) == text(predicted)


def identity_invariants(value):
    owners = {d['id'] for d in value['drafts']}
    proposals = {(p['draft_id'], p['version']): p['id'] for p in value['proposals']}
    for p in value['proposals']:
        payload = p['payload']
        assert p['draft_id'] in owners
        assert (p['id'], p['draft_id'], p['version']) == (payload['proposal_id'], payload['draft_id'], payload['version'])
        previous = proposals.get((p['draft_id'], p['version'] - 1))
        assert payload['previous_proposal_id'] == previous


def main():
    if OUT.exists():
        raise FileExistsError('Preserve the completed independent reconciliation')
    registration, summary = read(REPLAY / 'registration.json'), read(REPLAY / 'summary.json')
    assert sha(REPLAY / 'registration.json') == summary['registration_sha256']
    verified = {}
    for name, h in registration['source_sha256'].items():
        assert sha(ROOT / name) == h, name
        verified[name] = h
    for name, h in summary['artifact_sha256'].items():
        assert sha(REPLAY / name) == h, name
        verified['evidence/apparel_tau_replay_v1/' + name] = h
    cases = {c['id']: c for c in read(DATA / 'cases.json')}
    records = {(r['case_id'], r['condition']): r for r in summary['records']}
    assert len(records) == len(summary['records']) == 144
    assert set(records) == set(map(tuple, registration['jobs']))
    groups = {c: Counter() for c in registration['conditions']}
    tools, retained_failures, business_errors = Counter(), [], []
    allocations = 0
    for ident, condition in registration['jobs']:
        folder = REPLAY / 'runs' / (ident + '-' + condition)
        original_folder = DATA / 'runs' / (ident + '-' + condition)
        execution, original = read(original_folder / 'execution.json'), read(original_folder / 'result.json')
        score, record = read(folder / 'score.json'), records[(ident, condition)]
        assert all(record[k] == v for k, v in score.items())
        assert record['original_strict_task_passed'] == original['evaluation']['passed']
        assert record['original_failures'] == original['evaluation']['failures']
        assert sha(original_folder / 'execution.json') == original['execution_sha256'] == record['original_execution_sha256']
        initial = state(DATA / 'initial' / ident / 'operations.sqlite')
        predicted, reference, live = [state(p) for p in
            (folder / 'predicted.sqlite', folder / 'reference.sqlite', original_folder / 'operations.sqlite')]
        assert text(predicted) == text(live)
        assert reference_equivalent(predicted, reference)
        for key, value in (('predicted', predicted), ('reference', reference), ('live', live)):
            assert text(read(folder / (key + '_state.json'))) == text(value)
            assert digest(value) == score['snapshot_sha256'][key]
            identity_invariants(value)
        expected = cases[ident]['expected']['proposal']
        writes = int(expected in ('revise', 'infeasible'))
        assert len(predicted['proposals']) == len(initial['proposals']) + writes
        for table in ('metadata','inventory','drafts','approvals','transport_events','confirmations'):
            assert text(predicted[table]) == text(initial[table])
        step_checks = read(folder / 'step_checks.json')
        expected_steps = [t for t in execution['traces'] if t['kind'] == 'tool']
        with closing(connect(folder / 'predicted.sqlite')) as db:
            actual_steps = [json.loads(r[0]) for r in db.execute("SELECT payload FROM traces WHERE kind='agent_tool' ORDER BY id")]
        assert len(expected_steps) == len(actual_steps) == len(step_checks) == score['recorded_tools'] == score['replayed_tools']
        for i, (expected_step, actual, check) in enumerate(zip(expected_steps, actual_steps, step_checks), 1):
            keys = ('observation_id','tool','success','result')
            wanted, obtained = ({k: row[k] for k in keys} for row in (expected_step, actual))
            assert text(wanted) == text(obtained)
            assert text(expected_step['arguments']) == text(actual['arguments'])
            assert expected_step['role'] == actual['role']
            assert check == {'step': i, 'tool': expected_step['tool'], 'recorded_success': expected_step['success'],
                'expected_output_sha256': digest(wanted), 'actual_output_sha256': digest(obtained)}
            tools.update([actual['tool']])
            if not actual['success']:
                business_errors.append({'case_id': ident, 'condition': condition, 'observation': actual['observation_id'],
                    'tool': actual['tool'], 'error': actual['result']})
        task = read(folder / 'task.json')
        actions = task['evaluation_criteria']['actions']
        assert task['evaluation_criteria']['reward_basis'] == ['DB']
        if writes:
            seed = read(DATA / 'initial' / ident / 'seed.json')
            assert [a['name'] for a in actions] == ['apparel_operation'] * 2
            assert [a['arguments'] for a in actions] == [
                {'name': 'read_proposal', 'arguments': {'proposal_id': seed['old_id']}, 'actor': 'runtime'},
                {'name': 'prepare_proposal', 'arguments': {'expected_revision': initial['drafts'][0]['revision']}, 'actor': 'runtime'}]
        else:
            assert actions == []
        binding = read(folder / 'identity_allocations.json')
        initial_ids = {p['id'] for p in initial['proposals']}
        assert {b['proposal_id'] for b in binding['0']} == {p['id'] for p in predicted['proposals']} - initial_ids
        assert len(binding['0']) == len(binding['1']) == writes
        allocations += writes
        assert score['strict_replay_passed'] and score['exact_output_types_passed'] and score['live_state_match']
        assert score['target_db_match'] and score['reference_actions_succeeded'] and score['replay_fidelity_passed']
        assert score['tau_reward']['db_check']['db_match'] and score['tau_reward']['reward'] == 1.0
        assert score['replay_error'] is None and score['recorded_business_errors'] == sum(not t['success'] for t in actual_steps)
        groups[condition].update({k: score[k] for k in summary['groups'][condition] if k not in ('runs','original_strict_task_passed')})
        groups[condition].update(runs=1, original_strict_task_passed=int(original['evaluation']['passed']))
        if not original['evaluation']['passed']:
            retained_failures.append({'case_id': ident, 'condition': condition, 'original_failure': original['evaluation']['failures'],
                'final_database_correct': True, 'original_run_status': original['run_status']})
    assert {c: dict(v) for c, v in groups.items()} == summary['groups']
    assert summary['network_connection_attempts'] == 0 and summary['no_new_paid_rows_verified']
    with closing(sqlite3.connect((ROOT / 'evidence/api_budget.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        total, rows = db.execute('SELECT SUM(COALESCE(charged,reserved)),COUNT(*) FROM calls').fetchone()
    assert {'micro_cny': total, 'rows': rows} == summary['ledger_before'] == summary['ledger_after']
    verified.update({'evidence/apparel_tau_replay_v1/registration.json': sha(REPLAY / 'registration.json'),
        'evidence/apparel_tau_replay_v1/summary.json': sha(REPLAY / 'summary.json')})
    result = {'created_at': datetime.now(timezone.utc).isoformat(), 'audit_completed': True,
        'all_checks_passed': True, 'runs': 144, 'reexecuted_tool_calls': sum(tools.values()),
        'tool_counts': dict(tools), 'proposal_creations_bound': allocations,
        'retained_original_task_failures': retained_failures, 'retained_business_errors': business_errors,
        'groups': {c: dict(v) for c, v in groups.items()}, 'new_model_calls': 0, 'new_model_cost_cny': 0,
        'ledger_micro_cny': total, 'ledger_rows': rows, 'verified_file_count': len(verified),
        'verified_sha256': verified, 'audit_source_sha256': sha(Path(__file__)),
        'scope': 'Independent stdlib read of actual replay traces and SQLite states, source labels, IDs, outputs and ledger; does not rerun the adapter or evaluate free text.',
        'shared_document_hashes': 'Input hashes describe the state at replay time; later knowledge/README changes require a separate chained receipt.'}
    with OUT.open('x', encoding='utf-8') as f:
        f.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('verified_sha256','groups')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
