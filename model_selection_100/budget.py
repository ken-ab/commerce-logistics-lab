"""CNY 100 model-selection task within CNY 480 whole-project authorization."""
from contextlib import closing
from datetime import datetime, timezone
import uuid

from research.budget import BudgetExceeded, BudgetLedger, micros

PREFIX = 'model-selection-100:'


class SelectionBudget(BudgetLedger):
    def reserve(self, *, maximum_cny, purpose, model, price_version):
        policy = self.policy()
        if policy.get('state') != 'ready':
            raise RuntimeError('Provider and prices must be ready')
        if not purpose.startswith(PREFIX) or not model or not price_version:
            raise ValueError('This authorization is restricted to the new selection experiment')
        amount = micros(maximum_cny)
        ceiling = min(micros(480), micros(policy['total_limit']), micros(policy['automatic_spend_ceiling']))
        if not 0 < amount <= micros(policy['maximum_per_call_cny']):
            raise BudgetExceeded('Per-call bound requires review')
        call_id = str(uuid.uuid4())
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM controls WHERE key='halt'").fetchone():
                raise BudgetExceeded('Shared project ledger is halted')
            used = db.execute('SELECT COALESCE(SUM(COALESCE(charged,reserved)),0) FROM calls').fetchone()[0]
            if used + amount > ceiling:
                raise BudgetExceeded('Whole-project CNY 480 authorization would be exceeded')
            task_used=db.execute('SELECT COALESCE(SUM(COALESCE(charged,reserved)),0) FROM calls WHERE purpose LIKE ?',
                                 (PREFIX+'%',)).fetchone()[0]
            task_ceiling=min(micros(100),micros(policy.get('selection_task_limit_cny',100)))
            if task_used+amount>task_ceiling:
                raise BudgetExceeded('Model-selection task CNY 100 authorization would be exceeded')
            if purpose.startswith(PREFIX+'screen:'):
                initial_used=db.execute('SELECT COALESCE(SUM(COALESCE(charged,reserved)),0) FROM calls '
                    'WHERE purpose LIKE ? OR purpose LIKE ?',
                    (PREFIX+'screen:%',PREFIX+'calibration%')).fetchone()[0]
                if initial_used+amount>micros(policy.get('screen_and_calibration_limit_cny',82)):
                    raise BudgetExceeded('Initial screening allocation reached; preserve final validation budget')
            if db.execute('SELECT 1 FROM calls WHERE purpose=?', (purpose,)).fetchone():
                raise BudgetExceeded('This experiment request already has a reservation; no duplicate submission')
            db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,NULL,'reserved',NULL,NULL)",
                       (call_id, datetime.now(timezone.utc).isoformat(), purpose, model, price_version, amount))
        return call_id
