"""Versioned research drafts, explicit substitution approvals and atomic confirmation."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from apparel_fulfillment.data import ROOT, digest, load_world
from apparel_fulfillment.orders import OrderError, alternatives, check_order, normalize_request, substitution
from apparel_fulfillment.route_audit import audit_route
from apparel_fulfillment.transport import iso, load_corridor, plan_transport, validate_event


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ApparelStore:
    def __init__(self, path: Path | None = None, *, world=None, corridor=None):
        self.path = path or ROOT / "data/apparel_operations.sqlite"
        self.base_world = deepcopy(world if world is not None else load_world())
        self.corridor = deepcopy(corridor if corridor is not None else load_corridor())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inventory(sku TEXT PRIMARY KEY, quantity INTEGER NOT NULL, version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY, owner TEXT NOT NULL, request TEXT NOT NULL, selections TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS approvals(draft_id TEXT NOT NULL, approval_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(draft_id,approval_id));
                CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(draft_id,version));
                CREATE TABLE IF NOT EXISTS transport_events(event_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS confirmations(id TEXT PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE, proposal_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS traces(id INTEGER PRIMARY KEY, draft_id TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')
            source = digest({"world": self.base_world, "corridor": self.corridor})
            existing = db.execute("SELECT value FROM metadata WHERE key='source_digest'").fetchone()
            if existing and existing[0] != source:
                raise OrderError("This operation database belongs to a different source snapshot")
            if not existing:
                db.execute("INSERT INTO metadata VALUES ('source_digest',?)", (source,))
                db.executemany("INSERT INTO inventory VALUES (?,?,?)", [(sku, s["available_catalog_units"], s["version"]) for sku, s in self.base_world["stock"].items()])

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _draft(self, db, owner, draft_id):
        row = db.execute("SELECT * FROM drafts WHERE id=? AND owner=?", (draft_id, owner)).fetchone()
        if not row:
            raise OrderError("Unknown draft in this session")
        return {**dict(row), "request": json.loads(row["request"]), "selections": json.loads(row["selections"])}

    def _world(self, db):
        world = deepcopy(self.base_world)
        for row in db.execute("SELECT * FROM inventory"):
            world["stock"][row["sku"]].update(available_catalog_units=row["quantity"], version=row["version"])
        return world

    def _approvals(self, db, draft_id):
        return frozenset(row[0] for row in db.execute("SELECT approval_id FROM approvals WHERE draft_id=?", (draft_id,)))

    def _events(self, db):
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM transport_events ORDER BY event_id")]

    def _trace(self, db, draft_id, actor, kind, payload):
        db.execute("INSERT INTO traces(draft_id,actor,kind,payload,created_at) VALUES (?,?,?,?,?)",
                   (draft_id, actor, kind, dump(payload), iso(datetime.now(timezone.utc))))

    def create_draft(self, owner: str, request: dict):
        if not isinstance(owner, str) or not owner:
            raise OrderError("A session owner is required")
        request = normalize_request(request)
        ident = "APP-" + uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO drafts VALUES (?,?,?,?,?,?)", (ident, owner, dump(request), "[]", 1, iso(datetime.now(timezone.utc))))
            self._trace(db, ident, "user", "order_request", request)
        return self.view(owner, ident)

    def _editable(self, db, draft, expected_revision):
        if type(expected_revision) is not int or expected_revision != draft["revision"]:
            raise OrderError("Draft revision changed; read the current draft first")
        if db.execute("SELECT 1 FROM confirmations WHERE draft_id=?", (draft["id"],)).fetchone():
            raise OrderError("Confirmed research orders cannot be edited; create a new draft")

    def select(self, owner, draft_id, selections, *, expected_revision):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            draft = self._draft(db, owner, draft_id)
            self._editable(db, draft, expected_revision)
            checked = check_order(draft["request"], selections, self._world(db), approved_ids=self._approvals(db, draft_id))
            db.execute("UPDATE drafts SET selections=?,revision=revision+1 WHERE id=?", (dump(selections), draft_id))
            db.execute("UPDATE proposals SET state='superseded' WHERE draft_id=? AND state IN ('pending','needs_adjustment')", (draft_id,))
            self._trace(db, draft_id, "operator", "select_variants", {"selections": selections, "order_check": checked})
        return self.view(owner, draft_id)

    def approve_substitution(self, owner, draft_id, approval_id, *, expected_revision):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            draft = self._draft(db, owner, draft_id)
            self._editable(db, draft, expected_revision)
            world = self._world(db)
            checked = check_order(draft["request"], draft["selections"], world)
            change = next((change for change in checked["substitution_proposals"] if change["approval_id"] == approval_id), None)
            if change is None:
                raise OrderError("This exact substitution is not proposed by the current selection")
            if not db.execute("SELECT 1 FROM approvals WHERE draft_id=? AND approval_id=?", (draft_id, approval_id)).fetchone():
                db.execute("INSERT INTO approvals VALUES (?,?,?,?)", (draft_id, approval_id, dump(change), iso(datetime.now(timezone.utc))))
                db.execute("UPDATE drafts SET revision=revision+1 WHERE id=?", (draft_id,))
                db.execute("UPDATE proposals SET state='superseded' WHERE draft_id=? AND state IN ('pending','needs_adjustment')", (draft_id,))
                self._trace(db, draft_id, "user", "approve_specific_substitution", change)
        return self.view(owner, draft_id)

    def view(self, owner, draft_id):
        with closing(self.connect()) as db:
            draft = self._draft(db, owner, draft_id)
            checked = check_order(draft["request"], draft["selections"], self._world(db), approved_ids=self._approvals(db, draft_id))
            proposals = [{"state": row["state"], **json.loads(row["payload"])} for row in db.execute("SELECT * FROM proposals WHERE draft_id=? ORDER BY version", (draft_id,))]
            confirmation = db.execute("SELECT payload FROM confirmations WHERE draft_id=?", (draft_id,)).fetchone()
            approved = [json.loads(row[0]) for row in db.execute("SELECT payload FROM approvals WHERE draft_id=? ORDER BY approval_id", (draft_id,))]
            return {**draft, "order_check": checked, "proposals": proposals,
                    "approved_substitutions": approved,
                    "confirmation": json.loads(confirmation[0]) if confirmation else None}

    def alternatives(self, owner, draft_id, line_id):
        with closing(self.connect()) as db:
            draft = self._draft(db, owner, draft_id)
            return alternatives(draft["request"], line_id, self._world(db))

    def propose(self, owner, draft_id, *, expected_revision, now=None):
        now = now or datetime.now(timezone.utc)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            draft = self._draft(db, owner, draft_id)
            self._editable(db, draft, expected_revision)
            world, events = self._world(db), self._events(db)
            checked = check_order(draft["request"], draft["selections"], world, approved_ids=self._approvals(db, draft_id))
            if checked["status"] != "ready":
                self._trace(db, draft_id, "planner", "proposal_rejected_order_not_ready", checked)
                return {"status": "order_not_ready", "order_check": checked}
            route = plan_transport(checked, draft["request"].get("shipping"), world, corridor=self.corridor, events=events, now=now)
            audit = audit_route(checked, draft["request"].get("shipping"), route, world, self.corridor, events=events, now=now)
            if route["status"] in {"planned", "not_required"}:
                if not audit["passed"]:
                    raise OrderError("The independent route validator rejected this proposal")
            previous = db.execute("SELECT id,version FROM proposals WHERE draft_id=? ORDER BY version DESC LIMIT 1", (draft_id,)).fetchone()
            version = previous["version"] + 1 if previous else 1
            ident = "PROP-" + uuid.uuid4().hex
            payload = {"proposal_id": ident, "draft_id": draft_id, "version": version,
                       "previous_proposal_id": previous["id"] if previous else None,
                       "request_revision": draft["revision"], "order_check": checked, "route": route,
                       "independent_route_audit": audit,
                       "created_at": iso(now), "source_snapshot_digest": digest({"world": self.base_world, "corridor": self.corridor})}
            state = "pending" if route["status"] in {"planned", "not_required"} else "needs_adjustment"
            db.execute("UPDATE proposals SET state='superseded' WHERE draft_id=? AND state IN ('pending','needs_adjustment')", (draft_id,))
            db.execute("INSERT INTO proposals VALUES (?,?,?,?,?)", (ident, draft_id, version, state, dump(payload)))
            self._trace(db, draft_id, "planner", "proposal_version", {"state": state, **payload})
            return {"state": state, **payload}

    def _assess(self, db, draft, proposal_id, now):
        row = db.execute("SELECT * FROM proposals WHERE id=? AND draft_id=?", (proposal_id, draft["id"])).fetchone()
        if not row:
            raise OrderError("Unknown proposal in this order")
        proposal = json.loads(row["payload"])
        errors = []
        if row["state"] != "pending": errors.append("proposal_not_pending")
        latest = db.execute("SELECT MAX(version) FROM proposals WHERE draft_id=?", (draft["id"],)).fetchone()[0]
        if proposal["version"] != latest: errors.append("proposal_superseded")
        if proposal["request_revision"] != draft["revision"]: errors.append("request_revision_changed")
        world, events = self._world(db), self._events(db)
        checked = check_order(draft["request"], draft["selections"], world, approved_ids=self._approvals(db, draft["id"]))
        if checked["status"] != "ready": errors.append("order_no_longer_ready")
        if checked["request_digest"] != proposal["order_check"]["request_digest"] or checked["quantities_catalog_units"] != proposal["order_check"]["quantities_catalog_units"]:
            errors.append("selected_order_changed")
        if checked["source_versions"] != proposal["order_check"]["source_versions"]: errors.append("inventory_or_rule_snapshot_changed")
        if proposal["source_snapshot_digest"] != digest({"world": self.base_world, "corridor": self.corridor}): errors.append("source_snapshot_changed")
        audit = audit_route(checked, draft["request"].get("shipping"), proposal["route"], world, self.corridor, events=events, now=now)
        errors.extend(audit["violations"])
        return {"proposal_id": proposal_id, "version": proposal["version"], "valid": not errors,
                "violations": sorted(set(errors)), "checked_at": iso(now), "route_audit": audit}, checked, proposal

    def assess(self, owner, draft_id, proposal_id, *, now=None):
        with closing(self.connect()) as db:
            draft = self._draft(db, owner, draft_id)
            return self._assess(db, draft, proposal_id, now or datetime.now(timezone.utc))[0]

    def confirm(self, owner, draft_id, proposal_id, *, now=None):
        now = now or datetime.now(timezone.utc)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            draft = self._draft(db, owner, draft_id)
            previous = db.execute("SELECT proposal_id,payload FROM confirmations WHERE draft_id=?", (draft_id,)).fetchone()
            if previous:
                if previous["proposal_id"] != proposal_id:
                    raise OrderError("A different proposal has already been confirmed")
                return {**json.loads(previous["payload"]), "idempotent_replay": True}
            assessment, checked, proposal = self._assess(db, draft, proposal_id, now)
            if not assessment["valid"]:
                self._trace(db, draft_id, "user", "confirmation_rejected", assessment)
                return {"status": "rejected", "assessment": assessment}
            for sku, qty in checked["quantities_catalog_units"].items():
                updated = db.execute("UPDATE inventory SET quantity=quantity-?,version=version+1 WHERE sku=? AND quantity>=? AND version=?",
                                     (qty, sku, qty, checked["source_versions"][sku]["stock_version"]))
                if updated.rowcount != 1:
                    raise OrderError("Inventory changed during confirmation")
            ident = "SIM-APP-" + uuid.uuid4().hex[:16]
            result = {"status": "confirmed_simulation", "confirmation_id": ident, "draft_id": draft_id,
                      "proposal_id": proposal_id, "proposal_version": proposal["version"], "confirmed_at": iso(now),
                      "quantities_catalog_units": checked["quantities_catalog_units"], "route": proposal["route"],
                      "assessment": assessment, "notice": "Local simulation only; no payment, carrier booking or shipment."}
            db.execute("INSERT INTO confirmations VALUES (?,?,?,?)", (ident, draft_id, proposal_id, dump(result)))
            db.execute("UPDATE proposals SET state='confirmed' WHERE id=?", (proposal_id,))
            self._trace(db, draft_id, "user", "confirm_simulation", result)
            return result

    def add_transport_event(self, event):
        event = validate_event(event, self.corridor)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload FROM transport_events WHERE event_id=?", (event["event_id"],)).fetchone()
            if existing and json.loads(existing[0]) != event:
                raise OrderError("An event ID cannot be reused for different conditions")
            db.execute("INSERT OR IGNORE INTO transport_events VALUES (?,?)", (event["event_id"], dump(event)))
            return {"event": event, "duplicate": bool(existing), "scope": "Simulated corridor event"}

    def transport_events(self):
        with closing(self.connect()) as db:
            return self._events(db)

    def set_simulated_stock(self, sku, quantity, *, expected_version):
        if type(quantity) is not int or quantity < 0 or type(expected_version) is not int:
            raise OrderError("Invalid simulated inventory update")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE inventory SET quantity=?,version=version+1 WHERE sku=? AND version=?", (quantity, sku, expected_version))
            if changed.rowcount != 1: raise OrderError("Inventory version or SKU mismatch")
            return dict(db.execute("SELECT * FROM inventory WHERE sku=?", (sku,)).fetchone())

    def trace(self, owner, draft_id, actor, kind, payload):
        with closing(self.connect()) as db, db:
            self._draft(db, owner, draft_id)
            self._trace(db, draft_id, actor, kind, payload)

    def traces(self, owner, draft_id):
        with closing(self.connect()) as db:
            self._draft(db, owner, draft_id)
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in db.execute("SELECT * FROM traces WHERE draft_id=? ORDER BY id", (draft_id,))]
