"""Process-safe pre-call budget reservations. No model calls or credentials here."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import sqlite3
import uuid

MICRO = Decimal(1_000_000)


class BudgetExceeded(RuntimeError):
    pass


def micros(value: str | int | Decimal) -> int:
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("Cost must be finite and nonnegative")
    return int((number * MICRO).to_integral_value(rounding=ROUND_CEILING))


class BudgetLedger:
    """One ledger for generation, judges, retries, memory and candidate generation.

    Reservations survive crashes. Uncertain network outcomes retain their full reservation.
    Costs are conservative estimates unless independently reconciled with provider billing.
    """

    def __init__(self, path: Path, policy_path: Path):
        self.path = path
        self.policy_path = policy_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS calls ("
                       "id TEXT PRIMARY KEY, created_at TEXT NOT NULL, purpose TEXT NOT NULL, "
                       "model TEXT NOT NULL, price_version TEXT NOT NULL, reserved INTEGER NOT NULL, "
                       "charged INTEGER, status TEXT NOT NULL, usage TEXT, accounting_basis TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS controls (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def policy(self) -> dict:
        policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        if policy.get("currency") != "CNY":
            raise ValueError("This authorization is denominated in CNY")
        return policy

    def reserve(self, *, maximum_cny: str, purpose: str, model: str, price_version: str) -> str:
        policy = self.policy()
        if policy.get("state") != "ready":
            raise RuntimeError("Paid calls require a verified usable provider and price card")
        amount = micros(maximum_cny)
        if amount <= 0 or not all((purpose, model, price_version)):
            raise ValueError("Reservation requires a positive bound, model, price and purpose")
        # Never allow a local config edit to enlarge the user's explicit authorization.
        ceiling = min(micros(300), micros(policy["total_limit"]), micros(policy["automatic_spend_ceiling"]))
        if amount > micros(policy.get("maximum_per_call_cny", "5")):
            raise BudgetExceeded("Per-call bound exceeds the configured calibration limit")
        call_id = str(uuid.uuid4())
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM controls WHERE key='halt'").fetchone():
                raise BudgetExceeded("Paid calls are halted pending a cost-bound review")
            used = db.execute("SELECT COALESCE(SUM(COALESCE(charged,reserved)),0) FROM calls").fetchone()[0]
            if used + amount > ceiling:
                raise BudgetExceeded("Shared project budget would be exceeded")
            db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,NULL,'reserved',NULL,NULL)",
                       (call_id, datetime.now(timezone.utc).isoformat(), purpose, model, price_version, amount))
        return call_id

    def settle(self, call_id: str, *, cost_cny: str, usage: dict, basis: str = "conservative_token_estimate") -> None:
        amount = micros(cost_cny)
        if basis not in {"conservative_token_estimate", "verified_provider_invoice"}:
            raise ValueError("Unknown accounting basis")
        # Only aggregate token counts belong here, never prompts or raw provider responses.
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in usage.values()):
            raise ValueError("Usage must contain nonnegative integer token counts")
        overrun = False
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reserved,charged,status FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                raise KeyError(call_id)
            if row[2] == "settled":
                if row[1] != amount:
                    raise ValueError("Settled cost cannot be silently overwritten")
                return
            overrun = amount > row[0]
            db.execute("UPDATE calls SET charged=?,status='settled',usage=?,accounting_basis=? WHERE id=?",
                       (amount, json.dumps(usage, sort_keys=True), basis, call_id))
            if overrun:
                # The stop flag is committed atomically with the excessive cost. Other
                # processes cannot reserve between settlement and a separate file update.
                db.execute("INSERT OR REPLACE INTO controls VALUES ('halt','cost_bound_requires_review')")
        if overrun:
            raise BudgetExceeded("Actual cost exceeded reservation; paid calls have been disabled")

    def mark_uncertain(self, call_id: str) -> None:
        with closing(self.connect()) as db, db:
            count = db.execute("UPDATE calls SET status='uncertain' WHERE id=? AND charged IS NULL", (call_id,)).rowcount
            if not count:
                raise ValueError("Only an unsettled call can become uncertain")

    def summary(self) -> dict:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT status,COUNT(*),SUM(COALESCE(charged,reserved)) FROM calls GROUP BY status").fetchall()
            halted = db.execute("SELECT value FROM controls WHERE key='halt'").fetchone()
        total = sum(r[2] for r in rows)
        return {"currency": "CNY", "calls": sum(r[1] for r in rows),
                "accounted_and_reserved_cny": str(Decimal(total) / MICRO),
                "status_counts": {r[0]: r[1] for r in rows},
                "halt_reason": halted[0] if halted else None,
                "note": "Uncertain/reserved calls count at their full bound; estimates are not platform invoices."}
