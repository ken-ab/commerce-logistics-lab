"""Persisted, single-concurrency interactive agent jobs; simulations use a separate runner."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from threading import Lock
import uuid

from apparel_fulfillment.agent import ApparelAgent, ARMS, MODEL
from apparel_fulfillment.data import ROOT
from apparel_fulfillment.orders import OrderError
from apparel_fulfillment.interactive_state import InteractiveOperationAgent, checked_operation
from apparel_fulfillment.job_lock import DirectoryRunLock
from commerce_lab.jobs import process_identity
from research.provider_gate import ProviderGate, ProviderHeld, guarded_business_client


OWNERSHIP = 'directory-lock-v1'
ACTIVE_ERROR = 'An interactive agent run is already active; wait for its result'


class AgentJobs:
    def __init__(self, store, directory=None, agent_factory=ApparelAgent, *, state_agent_factory=InteractiveOperationAgent, client_factory=None, identity_lookup=process_identity):
        self.store = store
        self.directory = directory or ROOT / 'evidence/apparel_interactive'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.factory = agent_factory
        self.state_factory = state_agent_factory
        self.gate = ProviderGate(ROOT / 'evidence/provider_availability.sqlite') if agent_factory is ApparelAgent else None
        self.client_factory = client_factory
        self.identity_lookup = identity_lookup
        self.lock = Lock()
        self.active = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='apparel-interactive')

    def path(self, job_id):
        if not re.fullmatch(r'AJOB-[a-f0-9]{32}', job_id):
            raise OrderError('Unknown interactive job')
        return self.directory / (job_id + '.json')

    def save(self, value):
        path = self.path(value['job_id'])
        temporary = path.with_name('.' + path.stem + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as target:
                json.dump(value, target, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def owner_ended(self, record):
        """Legacy records have no execution lock; unknown liveness must not permit recovery."""
        pid = record.get('process_id')
        if type(pid) is not int or pid <= 0:
            return False
        if sys.platform != 'win32' and not sys.platform.startswith('linux'):
            return False  # Existing identity lookup supports Windows and Linux only.
        current = self.identity_lookup(pid)
        previous = record.get('process_identity')
        return current != 'unknown' and (current is None or
               previous not in (None, 'unknown') and current != previous)

    def recover_record(self, record):
        # Caller owns the directory lock. A new-protocol running record is now ownerless.
        if record['status'] == 'running' and (record.get('ownership') == OWNERSHIP or self.owner_ended(record)):
            record['status'] = 'interrupted'
            record['notice'] = 'The execution owner ended before saving a terminal result; partial tool traces and budget reservations remain. No automatic retry.'
            self.save(record)

    def start(self, owner, draft_id, task, arm, *, operation=None):
        self.store.view(owner, draft_id)
        if arm not in ARMS or not isinstance(task, str) or not 1 <= len(task) <= 4000:
            raise OrderError('Provide an execution policy and a bounded task')
        operation = checked_operation(self.store, owner, draft_id, operation)
        with self.lock:
            if self.active is not None:
                raise OrderError(ACTIVE_ERROR)
            execution_lock = DirectoryRunLock(self.directory)
            if not execution_lock.acquire():
                raise OrderError(ACTIVE_ERROR)
            saved = False
            try:
                for path in self.directory.glob('AJOB-*.json'):
                    existing = json.loads(path.read_text(encoding='utf-8'))
                    self.recover_record(existing)
                    if existing['status'] == 'running':
                        raise OrderError(ACTIVE_ERROR + '; a legacy owner is live or cannot be verified')
                client = self.client_factory() if self.client_factory else guarded_business_client(self.gate) if self.gate else None
                if client:
                    try:
                        client.ensure_available(MODEL)
                    except ProviderHeld as error:
                        raise OrderError(str(error)) from None
                record = {'job_id': 'AJOB-' + uuid.uuid4().hex, 'owner': owner, 'draft_id': draft_id,
                          'created_at': datetime.now(timezone.utc).isoformat(),
                          'task': task, 'arm': arm, 'operation': operation,
                          'status': 'running', 'process_id': os.getpid(), 'ownership': OWNERSHIP}
                if sys.platform == 'win32' or sys.platform.startswith('linux'):
                    record['process_identity'] = self.identity_lookup(os.getpid())
                self.save(record)
                saved = True
                self.active = record['job_id']
                self.pool.submit(self.work, record, client, execution_lock)
            except BaseException as error:
                try:
                    if saved:
                        record.update(status='failed', error_type=type(error).__name__)
                        self.save(record)
                finally:
                    self.active = None
                    execution_lock.release()
                raise
            return {key: record[key] for key in ('job_id', 'draft_id', 'arm', 'operation', 'status')}

    def work(self, record, client, execution_lock):
        try:
            options = {'client': client} if client is not None else {}
            factory = self.factory
            if record.get('operation') is not None:
                factory = self.state_factory
                options.update(contract=record['operation'], bootstrap=True, enforce_contract=True, phase='interactive_state_v3')
            result = factory(self.store, record['owner'], record['draft_id'], arm=record['arm'], **options).run(record['task'])
            record.update(status=result['run_status'], result=result)
            if self.gate and self.gate.status('aihubmix')['held']:
                record['notice'] = 'AIHubMix 账户服务不可用，已停止后续模型请求。当前失败与费用预留已保留；请恢复该服务。'
        except Exception as error:
            record.update(status='failed', error_type=type(error).__name__)
        finally:
            with self.lock:
                try:
                    self.save(record)
                finally:
                    self.active = None
                    execution_lock.release()

    def latest(self, owner, draft_id):
        self.store.view(owner, draft_id)
        candidates = []
        with self.lock:
            for path in self.directory.glob('AJOB-*.json'):
                record = json.loads(path.read_text(encoding='utf-8'))
                if record['owner'] != owner or record['draft_id'] != draft_id:
                    continue
                traces = (record.get('result') or {}).get('traces') or [{}]
                started = record.get('created_at') or traces[0].get('at')
                timestamp = datetime.fromisoformat(started.replace('Z', '+00:00')).timestamp() if started else path.stat().st_mtime
                candidates.append((timestamp, record['job_id']))
        return self.get(owner, max(candidates)[1]) if candidates else None

    def get(self, owner, job_id, *, full=False):
        with self.lock:
            path = self.path(job_id)
            if not path.exists(): raise OrderError('Unknown interactive job')
            record = json.loads(path.read_text(encoding='utf-8'))
            if record['owner'] != owner: raise OrderError('Unknown interactive job in this session')
            if record['status'] == 'running':
                execution_lock = DirectoryRunLock(self.directory)
                if execution_lock.acquire():
                    try:
                        # The writer may have completed between our initial read and lock acquisition.
                        record = json.loads(path.read_text(encoding='utf-8'))
                        self.recover_record(record)
                    finally:
                        execution_lock.release()
        if full:
            return record
        result = record.get('result')
        return {key: record.get(key) for key in ('job_id', 'draft_id', 'arm', 'operation', 'status', 'error_type', 'notice')} | {
            'result': {key: result[key] for key in ('run_id', 'run_status', 'report', 'model_calls', 'tool_calls', 'input_tokens',
                       'output_tokens', 'accounted_and_reserved_cny', 'latency_seconds', 'delegations', 'error_type')} if result else None}
