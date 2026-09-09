"""Freeze and execute one paired comparison; never silently rerun an attempted case."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import sys

from apparel_fulfillment.agent import ApparelAgent, ARMS, MODEL, VERSION
from apparel_fulfillment.data import ROOT, digest
from apparel_fulfillment.store import ApparelStore
from apparel_fulfillment.transport import instant
from research.apparel_cases import setup
from research.apparel_evaluation import score
from delivery_budget import operational_ledger

DIRECTORY = ROOT / 'evidence/apparel_strategy_v1'
FILES = ['apparel_fulfillment/agent.py', 'apparel_fulfillment/orders.py', 'apparel_fulfillment/store.py',
         'apparel_fulfillment/transport.py', 'apparel_fulfillment/route_audit.py', 'apparel_fulfillment/data.py',
         'data/apparel_fulfillment_v1.json', 'data/apparel_corridor_v1.json',
         'data/apparel_cases_validation_v1.json', 'data/apparel_cases_test_v1.json',
         'research/apparel_cases.py', 'research/apparel_evaluation.py', 'research/apparel_experiment.py',
         'research/APPAREL_EXPERIMENT_PROTOCOL.md', 'research/apparel_analysis.py',
         'logistics_lab/planning.py', 'research/model_client.py', 'research/model_config.py',
         'research/tls_transport.py', 'research/budget.py', 'research/rate_card.json',
         'delivery_budget.py', 'delivery_budget_policy.json', 'evidence/apparel_fixture_audit_v2/summary.json']


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)


def freeze():
    if (DIRECTORY / 'method.json').exists(): raise FileExistsError('A registered method already exists')
    audit = json.loads((ROOT / FILES[-1]).read_text(encoding='utf-8'))
    if audit['cases'] != 126 or audit['passed'] != 126: raise ValueError('All independent fixture checks must pass before registration')
    import numpy, pydantic
    value = {'registered_at': datetime.now(timezone.utc).isoformat(), 'version': VERSION, 'model': MODEL,
             'runtime': {'python': sys.version, 'numpy': numpy.__version__, 'pydantic': pydantic.__version__, 'sqlite': sqlite3.sqlite_version},
             'arms': ARMS, 'validation_cases': 18, 'test_cases': 108, 'workers': 3,
             'seed': 20260908, 'bootstrap_seed': 260908, 'bootstrap_iterations': 10000,
             'budget_at_registration': operational_ledger().summary(),
             'method_files_sha256': {name: sha(ROOT / name) for name in FILES}}
    save(DIRECTORY / 'method.json', value)
    print(json.dumps({'registered': str(DIRECTORY / 'method.json'), 'sha256': sha(DIRECTORY / 'method.json')}, ensure_ascii=False))


def validate():
    value = json.loads((DIRECTORY / 'method.json').read_text(encoding='utf-8'))
    changed = [name for name, expected in value['method_files_sha256'].items() if sha(ROOT / name) != expected]
    if changed: raise ValueError('Registered method changed: ' + ', '.join(changed))
    return value


def execute(case, arm, initial, folder):
    result_path = folder / 'result.json'
    if result_path.exists():
        return {'case_id': case['id'], 'arm': arm, 'status': 'already_recorded'}
    if (folder / 'attempt.json').exists():
        raise RuntimeError('An attempted run has no final record. Preserve partial evidence; do not automatically retry: ' + str(folder))
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'attempt.json').open('x', encoding='utf-8') as handle:
        json.dump({'case_id': case['id'], 'arm': arm, 'pid': os.getpid(), 'stage': 'initializing'}, handle)
    world = json.loads((initial / 'world.json').read_text(encoding='utf-8'))
    seed = json.loads((initial / 'initial.json').read_text(encoding='utf-8'))
    with closing(sqlite3.connect(initial / 'operations.sqlite')) as src, closing(sqlite3.connect(folder / 'operations.sqlite')) as dst:
        src.backup(dst)
    store = ApparelStore(folder / 'operations.sqlite', world=world)
    now = instant(seed['now'])
    agent = ApparelAgent(store, 'evaluation', seed['draft_id'], arm=arm, now=now, phase=case['partition'])
    marker = {'case_id': case['id'], 'arm': arm, 'run_id': agent.run_id, 'purpose': agent.purpose,
              'pid': os.getpid(), 'method_sha256': sha(DIRECTORY / 'method.json'),
              'initial_state_digest': seed['initial_state_digest'], 'at': datetime.now(timezone.utc).isoformat()}
    save(folder / 'attempt.json', marker)
    result = agent.run(case['task'])
    result.update(case_id=case['id'], family=case['family'], target_sku=case['target_sku'],
                  partition=case['partition'], method_sha256=marker['method_sha256'], initial_state_digest=seed['initial_state_digest'])
    result['score'] = score(case, result, store, now)
    save(result_path, result)
    print(json.dumps({key: result[key] for key in ('case_id', 'arm', 'run_status', 'model_calls', 'accounted_and_reserved_cny')} |
                     {'task_completed': result['score']['task_completed']}, ensure_ascii=False), flush=True)
    return {key: result[key] for key in ('case_id', 'arm', 'run_status')}


def run(partition):
    method = validate()
    if partition == 'test':
        validation = json.loads((DIRECTORY / 'validation/summary.json').read_text(encoding='utf-8'))
        if validation.get('status') != 'complete' or not (DIRECTORY / 'validation/selection.json').exists():
            raise ValueError('Complete validation and record the default strategy before final test')
    data = json.loads((ROOT / f'data/apparel_cases_{partition}_v1.json').read_text(encoding='utf-8'))
    jobs = []
    rng = random.Random(method['seed'] + (1 if partition == 'test' else 0))
    for case in data['cases']:
        parent = DIRECTORY / partition / case['id']
        initial = parent / 'initial'
        if not (initial / 'initial.json').exists():
            if initial.exists(): raise RuntimeError('Incomplete initial fixture; inspect before continuing')
            store, draft_id, now = setup(case, initial / 'operations.sqlite')
            initial_view = store.view('evaluation', draft_id)
            save(initial / 'world.json', store.base_world)
            save(initial / 'initial.json', {'draft_id': draft_id, 'now': now.isoformat(), 'initial_state_digest': digest(initial_view)})
        arms = list(ARMS); rng.shuffle(arms)
        jobs.extend((case, arm, initial, parent / arm) for arm in arms)
    save(DIRECTORY / partition / 'schedule.json', {'jobs': [{'case_id': c['id'], 'arm': a} for c, a, _, _ in jobs],
         'workers': method['workers'], 'method_sha256': sha(DIRECTORY / 'method.json')})
    with ThreadPoolExecutor(max_workers=method['workers'], thread_name_prefix='apparel-study') as pool:
        futures = [pool.submit(execute, *job) for job in jobs]
        for future in as_completed(futures): future.result()
    validate()
    from research.apparel_analysis import analyze
    analyze(partition)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['freeze', 'validation', 'test'])
    args = parser.parse_args()
    freeze() if args.action == 'freeze' else run(args.action)
