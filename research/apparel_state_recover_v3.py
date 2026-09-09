"""Preserve interrupted attempts as failures; continue only unstarted jobs."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3
import subprocess
import sys

from apparel_fulfillment.agent import MODEL
from apparel_fulfillment.agent_state_v3 import VERSION
from apparel_fulfillment.data import ROOT, digest
from apparel_fulfillment.store import ApparelStore
from apparel_fulfillment.transport import instant, load_corridor
from research.apparel_experiment import save, sha
from research.apparel_state_evaluation_v3 import evaluate
from research.apparel_state_pilot_v3 import DIRECTORY, CONDITIONS, validate_pilot, execute, summarize

PAUSE = ROOT / 'evidence/apparel_state_v3_interruption'


class ReadOnlyStore(ApparelStore):
    def __init__(self, path, world):
        self.path, self.base_world, self.corridor = path, deepcopy(world), load_corridor()

    def connect(self):
        db = sqlite3.connect('file:' + self.path.as_posix() + '?mode=ro', uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        return db


def no_original_process():
    command = "@(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -like '*research.apparel_state_pilot_v3 run*' }).Count"
    outcome = subprocess.run(['powershell', '-NoProfile', '-Command', command], capture_output=True, text=True, check=True)
    if outcome.stdout.strip() != '0': raise RuntimeError('An original pilot process is still live; do not recover or relaunch')


def recover_result(case, condition, arm, attempt, seed, store, traces, ledger_rows, manifest_sha):
    starts = [t for t in traces if t['kind'] == 'start']
    if len(starts) != 1: raise ValueError('Exactly one durable start required')
    if any(t['kind'] == 'completed' for t in traces): raise ValueError('A completed marker must be investigated, not relabeled interrupted')
    before = starts[0]['initial_state']
    if digest(before) != seed['initial_view_digest']: raise ValueError('Initial state differs')
    observations = {}
    calls_by_id = {}
    for trace in traces:
        if trace['kind'] == 'tool':
            ident = trace['observation_id']
            observations[ident] = {'observation_id': ident, 'tool': trace['tool'], 'success': trace['success'], 'result': trace['result']}
        elif trace['kind'] == 'citation_directory':
            observations[trace['observation_id']]['citation_directory'] = trace['directory']
        elif trace['kind'] == 'model':
            call = {k: value for k, value in trace['response'].items() if k != 'message'}
            calls_by_id[call['budget_call_id']] = call
    calls = []
    for row in ledger_rows:
        if row['id'] in calls_by_id:
            calls.append(calls_by_id.pop(row['id']))
        else:
            calls.append({'status': 'interrupted_unknown', 'role': None, 'budget_call_id': row['id'],
                          'requested_model': row['model'], 'returned_model': None,
                          'usage': json.loads(row['usage']) if row['usage'] else None,
                          'estimated_cost_cny': str(Decimal(row['charged'])/1000000) if row['charged'] is not None else None,
                          'latency_seconds': None,
                          'notice': 'Ledger reservation survived but no response payload was durably observed. No success or returned text inferred.'})
    if calls_by_id: raise ValueError('Observed model response has no matching ledger entry')
    times = [instant(t['at']) for t in traces] + [instant(r['created_at']) for r in ledger_rows]
    b, g = CONDITIONS[condition]
    accounted = sum(r['charged'] if r['charged'] is not None else r['reserved'] for r in ledger_rows)
    result = {'run_id': attempt['run_id'], 'arm': arm, 'model': MODEL, 'policy_version': VERSION,
              'run_status': 'interrupted', 'error_type': 'ProcessExitedWithoutResult', 'report': None,
              'model_calls': len(calls), 'successful_model_calls': sum(c['status'] == 'success' for c in calls),
              'tool_calls': len(observations), 'successful_tool_calls': sum(o['success'] for o in observations.values()),
              'input_tokens': sum((c.get('usage') or {}).get('prompt_tokens', 0) for c in calls),
              'output_tokens': sum((c.get('usage') or {}).get('completion_tokens', 0) for c in calls),
              'settled_cost_cny': str(sum((Decimal(r['charged'])/1000000 for r in ledger_rows if r['charged'] is not None), Decimal(0))),
              'accounted_and_reserved_cny': str(Decimal(accounted)/1000000),
              'latency_seconds': max(0.0, (max(times)-instant(starts[0]['at'])).total_seconds()),
              'latency_observation': 'lower_bound_censored',
              'model_latency_seconds': sum(c.get('latency_seconds') or 0 for c in calls),
              'delegations': sum(t['kind'] == 'delegation' for t in traces), 'observations': observations,
              'calls': calls, 'before': before, 'after': store.view('evaluation', seed['draft_id']), 'traces': traces,
              'operation_contract': case['contract'], 'interventions': {'bootstrap': b, 'enforce_contract': g},
              'host_initial_reads': sum(t['kind'] == 'tool' and t['role'] == 'runtime' for t in traces),
              'host_completion_checks': sum(t['kind'] == 'operation_check' for t in traces),
              'case_id': case['id'], 'scenario': case['scenario'], 'family': case['family'], 'target_sku': case['target_sku'],
              'condition': condition, 'partition': 'development_only', 'initial_view_digest': seed['initial_view_digest'],
              'registration_sha256': attempt['registration_sha256'], 'interruption_manifest_sha256': manifest_sha,
              'recovery_notice': 'Offline reconstruction of durable partial execution; no rerun, no final model report, incomplete timing. Full uncertain reservations retained.'}
    return result


def snapshot():
    no_original_process(); registration = validate_pilot()
    if PAUSE.exists(): raise FileExistsError('Preserve existing interruption snapshot')
    PAUSE.mkdir(parents=True)
    incomplete, complete, ledger_entries = [], {}, {}
    with closing(sqlite3.connect('file:' + (ROOT / 'evidence/api_budget.sqlite').as_posix() + '?mode=ro', uri=True)) as ledger:
        ledger.row_factory = sqlite3.Row
        for case_id, condition, arm in registration['jobs']:
            folder = DIRECTORY / 'runs' / case_id / condition / arm
            if (folder / 'result.json').exists():
                complete[(folder / 'result.json').relative_to(ROOT).as_posix()] = sha(folder / 'result.json')
                continue
            if not (folder / 'attempt.json').exists(): continue
            if (folder / 'execution.json').exists(): raise ValueError('Existing full execution needs scoring-only recovery')
            attempt = json.loads((folder / 'attempt.json').read_text(encoding='utf-8'))
            destination = PAUSE / 'partial' / case_id / condition / arm; destination.mkdir(parents=True)
            with closing(sqlite3.connect('file:' + (folder / 'operations.sqlite').as_posix() + '?mode=ro', uri=True)) as src:
                with closing(sqlite3.connect(destination / 'operations.sqlite')) as dst: src.backup(dst)
            save(destination / 'attempt.json', attempt)
            records = [dict(row) for row in ledger.execute('SELECT * FROM calls WHERE purpose LIKE ? ORDER BY created_at', (attempt['purpose']+'%',))]
            save(destination / 'ledger.json', records)
            incomplete.append({'case_id': case_id, 'condition': condition, 'arm': arm,
                               'backup': destination.relative_to(ROOT).as_posix(),
                               'original_database_sha256': sha(folder / 'operations.sqlite'),
                               'snapshot_sha256': {n: sha(destination/n) for n in ('operations.sqlite', 'attempt.json', 'ledger.json')}})
    files = ('research/apparel_state_recover_v3.py', 'research/APPAREL_STATE_V3_INTERRUPTION.md',
             'tests/test_apparel_state_recover_v3.py', 'evidence/apparel_state_recover_v3_tests.xml')
    manifest = {'observed_at': datetime.now(timezone.utc).isoformat(), 'original_process_live': False,
                'original_session': 86831, 'complete_result_sha256': complete, 'interrupted': incomplete,
                'unstarted': registration['expected_runs']-len(complete)-len(incomplete),
                'registration_sha256': sha(DIRECTORY/'registration.json'),
                'recovery_files_sha256': {name: sha(ROOT/name) for name in files},
                'notice': 'Preserve interrupted runs as failures; only unstarted jobs may execute. See timing amendment.'}
    save(PAUSE/'manifest.json', manifest)
    print(json.dumps({'complete': len(complete), 'interrupted': len(incomplete), 'unstarted': manifest['unstarted'],
                      'manifest_sha256': sha(PAUSE/'manifest.json')}))


def restore():
    no_original_process(); validate_pilot()
    manifest = json.loads((PAUSE/'manifest.json').read_text(encoding='utf-8'))
    for name, expected in (manifest['complete_result_sha256'] | manifest['recovery_files_sha256']).items():
        if sha(ROOT/name) != expected: raise ValueError('Interrupted-phase evidence changed: '+name)
    cases = {c['id']: c for c in json.loads((DIRECTORY/'cases.json').read_text(encoding='utf-8'))['cases']}
    for item in manifest['interrupted']:
        case_id, condition, arm = item['case_id'], item['condition'], item['arm']
        folder = DIRECTORY/'runs'/case_id/condition/arm; backup = ROOT/item['backup']
        if (folder/'execution.json').exists() or (folder/'result.json').exists(): raise FileExistsError('Do not overwrite a restored attempt')
        if sha(folder/'operations.sqlite') != item['original_database_sha256']: raise ValueError('Original partial state changed')
        for name, expected in item['snapshot_sha256'].items():
            if sha(backup/name) != expected: raise ValueError('Interruption snapshot changed')
        initial = DIRECTORY/'initial'/case_id
        world = json.loads((initial/'world.json').read_text(encoding='utf-8'))
        seed = json.loads((initial/'seed.json').read_text(encoding='utf-8'))
        attempt = json.loads((backup/'attempt.json').read_text(encoding='utf-8'))
        store = ReadOnlyStore(backup/'operations.sqlite', world)
        trace_rows = store.traces('evaluation', seed['draft_id'])
        traces = [{'kind': row['kind'].removeprefix('agent_'), **{k:v for k,v in row['payload'].items() if k!='run_id'}} for row in trace_rows
                  if row['kind'].startswith('agent_') and row['payload'].get('run_id') == attempt['run_id']]
        records = json.loads((backup/'ledger.json').read_text(encoding='utf-8'))
        result = recover_result(cases[case_id], condition, arm, attempt, seed, store, traces, records, sha(PAUSE/'manifest.json'))
        save(folder/'execution.json', result)
        result.update(evaluate(cases[case_id], result, store, instant(seed['now'])))
        save(folder/'result.json', result)
    print(json.dumps({'restored_as_interrupted': len(manifest['interrupted']), 'new_model_calls': 0}))


def resume():
    no_original_process(); registration = validate_pilot()
    manifest = json.loads((PAUSE/'manifest.json').read_text(encoding='utf-8'))
    for name, expected in (manifest['complete_result_sha256'] | manifest['recovery_files_sha256']).items():
        if sha(ROOT/name) != expected: raise ValueError('Frozen evidence changed')
    for item in manifest['interrupted']:
        p = DIRECTORY/'runs'/item['case_id']/item['condition']/item['arm']/'result.json'
        r = json.loads(p.read_text(encoding='utf-8'))
        if r['run_status'] != 'interrupted' or r['report'] is not None: raise ValueError('An interrupted result must remain failed')
    pending = []
    cases = {c['id']:c for c in json.loads((DIRECTORY/'cases.json').read_text(encoding='utf-8'))['cases']}
    for case, condition, arm in registration['jobs']:
        folder = DIRECTORY/'runs'/case/condition/arm
        if (folder/'result.json').exists(): continue
        if (folder/'attempt.json').exists(): raise RuntimeError('A new interrupted attempt needs separate accounting')
        pending.append((case, condition, arm))
    if not pending: raise ValueError('No unstarted jobs remain')
    with ThreadPoolExecutor(max_workers=registration['max_workers']) as pool:
        futures = [pool.submit(execute, cases[c], condition, arm) for c, condition, arm in pending]
        for f in as_completed(futures): print(json.dumps(f.result(),ensure_ascii=False),flush=True)
    summarize()


if __name__ == '__main__': {'snapshot': snapshot, 'restore': restore, 'resume': resume}[sys.argv[1]]()
