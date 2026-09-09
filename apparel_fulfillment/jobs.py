"""Persisted, single-concurrency interactive agent jobs; simulations use a separate runner."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from threading import Lock
import uuid

from apparel_fulfillment.agent import ApparelAgent, ARMS, MODEL
from apparel_fulfillment.data import ROOT
from apparel_fulfillment.orders import OrderError
from apparel_fulfillment.interactive_state import InteractiveOperationAgent, checked_operation
from research.provider_gate import ProviderGate, ProviderHeld, guarded_business_client


class AgentJobs:
    def __init__(self, store, directory=None, agent_factory=ApparelAgent, *, state_agent_factory=InteractiveOperationAgent, client_factory=None):
        self.store = store
        self.directory = directory or ROOT / 'evidence/apparel_interactive'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.factory = agent_factory
        self.state_factory = state_agent_factory
        self.gate = ProviderGate(ROOT / 'evidence/provider_availability.sqlite') if agent_factory is ApparelAgent else None
        self.client_factory = client_factory
        self.lock = Lock()
        self.active = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='apparel-interactive')

    def path(self, job_id):
        if not re.fullmatch(r'AJOB-[a-f0-9]{32}', job_id):
            raise OrderError('Unknown interactive job')
        return self.directory / (job_id + '.json')

    def save(self, value):
        path = self.path(value['job_id'])
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)

    def start(self, owner, draft_id, task, arm, *, operation=None):
        self.store.view(owner, draft_id)
        if arm not in ARMS or not isinstance(task, str) or not 1 <= len(task) <= 4000:
            raise OrderError('Provide an execution policy and a bounded task')
        operation = checked_operation(self.store, owner, draft_id, operation)
        with self.lock:
            if self.active is not None:
                raise OrderError('An interactive agent run is already active; wait for its result')
            client = self.client_factory() if self.client_factory else guarded_business_client(self.gate) if self.gate else None
            if client:
                try:
                    client.ensure_available(MODEL)
                except ProviderHeld as error:
                    raise OrderError(str(error)) from None
            record = {'job_id': 'AJOB-' + uuid.uuid4().hex, 'owner': owner, 'draft_id': draft_id,
                      'created_at': datetime.now(timezone.utc).isoformat(),
                      'task': task, 'arm': arm, 'operation': operation,
                      'status': 'running', 'process_id': os.getpid()}
            self.active = record['job_id']
            self.save(record)
            self.pool.submit(self.work, record, client)
            return {key: record[key] for key in ('job_id', 'draft_id', 'arm', 'operation', 'status')}

    def work(self, record, client=None):
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
                self.save(record)
                self.active = None

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
            if record['status'] == 'running' and record['process_id'] != os.getpid():
                record['status'] = 'interrupted'
                record['notice'] = 'The service restarted; partial tool traces and budget reservations remain. No automatic retry.'
                self.save(record)
        if full:
            return record
        result = record.get('result')
        return {key: record.get(key) for key in ('job_id', 'draft_id', 'arm', 'operation', 'status', 'error_type', 'notice')} | {
            'result': {key: result[key] for key in ('run_id', 'run_status', 'report', 'model_calls', 'tool_calls', 'input_tokens',
                       'output_tokens', 'accounted_and_reserved_cny', 'latency_seconds', 'delegations', 'error_type')} if result else None}
