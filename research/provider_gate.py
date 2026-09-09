"""Durable provider holds for future jobs; no retries or changes to frozen clients.

Checks occur before the budgeted client reserves/sends. Requests already on the
wire may finish, but another request after a recorded hold is rejected locally.
"""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from research.model_client import ModelCallError


class ProviderHeld(ModelCallError):
    pass


def account_failure(error):
    message = str(error).lower()
    if 'http 401' in message:
        return 'authentication_rejected'
    if 'http 402' in message:
        return 'payment_required'
    if 'http 400' in message and ('overdue-payment' in message or 'arrearage' in message):
        return 'payment_required'
    if 'http 403' in message and any(s in message for s in (
            'account balance is insufficient', 'insufficient balance', 'insufficient credit')):
        return 'account_balance_insufficient'
    return None


class ProviderGate:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS holds (provider TEXT PRIMARY KEY, reason TEXT NOT NULL, at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS events (provider TEXT, action TEXT, reason TEXT, at TEXT)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def status(self, provider):
        with closing(self.connect()) as db:
            row = db.execute('SELECT reason,at FROM holds WHERE provider=?', (provider,)).fetchone()
        return {'provider': provider, 'held': bool(row), 'reason': row[0] if row else None,
                'at': row[1] if row else None}

    def check(self, provider):
        state = self.status(provider)
        if state['held']:
            raise ProviderHeld(f"{provider} 模型服务已暂停：{state['reason']}。恢复服务后再解除暂停；本次没有发送模型请求或预留费用。")

    def hold(self, provider, reason):
        if reason not in {'authentication_rejected', 'payment_required', 'account_balance_insufficient'}:
            raise ValueError('Only classified account errors can create a hold')
        at = datetime.now(timezone.utc).isoformat()
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute('INSERT OR IGNORE INTO holds VALUES (?,?,?)', (provider, reason, at))
            if cursor.rowcount:
                db.execute('INSERT INTO events VALUES (?,?,?,?)', (provider, 'hold', reason, at))

    def acknowledge_recovery(self, provider):
        """Local operator action after the user reports service recovery; no probe/retry."""
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM holds WHERE provider=?', (provider,))
            db.execute('INSERT INTO events VALUES (?,?,?,?)',
                       (provider, 'recovery_acknowledged', 'user_reported_recovery', datetime.now(timezone.utc).isoformat()))


class GuardedChatClient:
    def __init__(self, client, gate):
        self.client, self.gate = client, gate

    def __getattr__(self, name):
        return getattr(self.client, name)

    def provider_for(self, model=None):
        return self.client.card['models'][model or self.client.config['COMMERCE_MODEL']]['provider']

    def ensure_available(self, model=None):
        self.gate.check(self.provider_for(model))

    def chat(self, messages, **kwargs):
        provider = self.provider_for(kwargs.get('model'))
        self.gate.check(provider)
        try:
            return self.client.chat(messages, **kwargs)
        except ModelCallError as error:
            reason = account_failure(error)
            if reason:
                self.gate.hold(provider, reason)
            raise


def guarded_business_client(gate=None):
    from delivery_budget import business_client
    from research.model_config import ROOT
    return GuardedChatClient(business_client(), gate or ProviderGate(ROOT / 'evidence/provider_availability.sqlite'))
