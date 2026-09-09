"""Current local-app authorization, separate from frozen experiment budgets."""
from contextlib import closing
from datetime import datetime, timezone
import uuid

from research.budget import BudgetExceeded, BudgetLedger, micros
from research.model_client import BudgetedChatClient
from research.model_config import ROOT


class OperationalBudget(BudgetLedger):
    def reserve(self, *, maximum_cny, purpose, model, price_version):
        if not purpose.startswith(('commerce_', 'commerce-interactive-rerank:v2:')):
            raise ValueError('This budget is restricted to local application requests')
        policy=self.policy();amount=micros(maximum_cny)
        if policy.get('state')!='ready' or not model or not price_version:
            raise ValueError('A ready verified model configuration is required')
        if not 0<amount<=micros(policy['maximum_per_call_cny']):
            raise BudgetExceeded('Local application per-call budget exceeded')
        ceiling=min(micros(480),micros(policy['total_limit']),micros(policy['automatic_spend_ceiling']))
        identity=str(uuid.uuid4())
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM controls WHERE key='halt'").fetchone():
                raise BudgetExceeded('Project calls are halted')
            used=db.execute('SELECT COALESCE(SUM(COALESCE(charged,reserved)),0) FROM calls').fetchone()[0]
            if used+amount>ceiling:
                raise BudgetExceeded('Whole-project CNY 480 authorization would be exceeded')
            db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,NULL,'reserved',NULL,NULL)",
                       (identity,datetime.now(timezone.utc).isoformat(),purpose,model,price_version,amount))
        return identity


def operational_ledger():
    return OperationalBudget(ROOT/'evidence/api_budget.sqlite',ROOT/'delivery_budget_policy.json')


def business_client():
    client=BudgetedChatClient()
    client.ledger=operational_ledger()
    return client
