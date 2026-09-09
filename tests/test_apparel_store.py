from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from apparel_fulfillment.orders import OrderError
from apparel_fulfillment.store import ApparelStore
from test_apparel_orders import fixture, request
from test_apparel_transport import NOW, SHIP


class ApparelStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="apparel-store-")
        self.world = fixture()
        for variant in self.world["variants"].values():
            variant["provenance"]["weight_grams_per_catalog_unit"] = {"evidence_id": "sim-weight"}
        self.store = ApparelStore(Path(self.temp.name) / "operations.sqlite", world=self.world)

    def tearDown(self):
        target = Path(self.temp.name).resolve()
        assert target.parent == Path(tempfile.gettempdir()).resolve() and target.name.startswith("apparel-store-")
        self.temp.cleanup()

    def draft(self, *, shipping=False):
        req = request(); req["needs_shipping"] = shipping
        if shipping: req["shipping"] = deepcopy(SHIP)
        draft = self.store.create_draft("owner", req)
        return self.store.select("owner", draft["id"], [{"line_id": "one", "sku": "A"}], expected_revision=draft["revision"])

    def test_versions_preserve_old_route_after_cancellation(self):
        draft = self.draft(shipping=True)
        old = self.store.propose("owner", draft["id"], expected_revision=draft["revision"], now=NOW)
        flight = old["route"]["segments"][1]
        event = {"event_id": "cancel-1", "kind": "cancel", "leg_id": flight["leg_id"],
                 "nominal_departure": flight["nominal_departure"], "published_at": "2026-09-08T00:00:00Z"}
        self.store.add_transport_event(event)
        rejected = self.store.confirm("owner", draft["id"], old["proposal_id"], now=NOW)
        self.assertEqual(rejected["status"], "rejected")
        new = self.store.propose("owner", draft["id"], expected_revision=draft["revision"], now=NOW)
        self.assertEqual(new["version"], 2)
        self.assertEqual(new["previous_proposal_id"], old["proposal_id"])
        view = self.store.view("owner", draft["id"])
        self.assertEqual(view["proposals"][0]["route"], old["route"])
        self.assertEqual(view["proposals"][0]["state"], "superseded")
        self.assertTrue(self.store.assess("owner", draft["id"], new["proposal_id"], now=NOW)["valid"])
        self.assertIn("confirmation_rejected", [t["kind"] for t in self.store.traces("owner", draft["id"])])

    def test_inventory_change_invalidates_pending_confirmation(self):
        draft = self.draft()
        proposal = self.store.propose("owner", draft["id"], expected_revision=draft["revision"], now=NOW)
        self.store.set_simulated_stock("A", 19, expected_version=1)
        result = self.store.confirm("owner", draft["id"], proposal["proposal_id"], now=NOW)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("inventory_or_rule_snapshot_changed", result["assessment"]["violations"])

    def test_duplicate_confirm_is_idempotent_and_two_orders_cannot_oversell(self):
        first, second = self.draft(), self.draft()
        p1 = self.store.propose("owner", first["id"], expected_revision=first["revision"], now=NOW)
        p2 = self.store.propose("owner", second["id"], expected_revision=second["revision"], now=NOW)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.store.confirm, "owner", draft["id"], proposal["proposal_id"], now=NOW)
                       for draft, proposal in ((first, p1), (second, p2))]
            results = [future.result() for future in futures]
        self.assertEqual(sorted(result["status"] for result in results), ["confirmed_simulation", "rejected"])
        win = next(result for result in results if result["status"] == "confirmed_simulation")
        repeat = self.store.confirm("owner", win["draft_id"], win["proposal_id"], now=NOW)
        self.assertTrue(repeat["idempotent_replay"])
        self.assertEqual(repeat["confirmation_id"], win["confirmation_id"])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT quantity FROM inventory WHERE sku='A'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0], 1)

    def test_approval_must_match_current_selection_and_revision(self):
        draft = self.draft()
        draft = self.store.select("owner", draft["id"], [{"line_id": "one", "sku": "B"}], expected_revision=draft["revision"])
        self.assertEqual(self.store.propose("owner", draft["id"], expected_revision=draft["revision"], now=NOW)["status"], "order_not_ready")
        approval = draft["order_check"]["substitution_proposals"][0]["approval_id"]
        with self.assertRaises(OrderError):
            self.store.approve_substitution("owner", draft["id"], "not-proposed", expected_revision=draft["revision"])
        current = self.store.approve_substitution("owner", draft["id"], approval, expected_revision=draft["revision"])
        self.assertEqual(current["order_check"]["status"], "ready")
        with self.assertRaises(OrderError):
            self.store.propose("owner", draft["id"], expected_revision=draft["revision"], now=NOW)

    def test_owner_isolation_and_source_snapshot_binding(self):
        draft = self.draft()
        with self.assertRaises(OrderError): self.store.view("other", draft["id"])
        other = deepcopy(self.world); other["stock"]["A"]["available_catalog_units"] = 50
        with self.assertRaises(OrderError): ApparelStore(self.store.path, world=other)

    def test_duplicate_event_keeps_one_record_and_conflict_is_rejected(self):
        event = {"event_id": "delay-1", "kind": "delay", "leg_id": "HK-EU-AIR", "delay_minutes": 30,
                 "nominal_departure": "2026-09-08T16:00:00Z", "published_at": "2026-09-08T00:00:00Z"}
        self.assertFalse(self.store.add_transport_event(event)["duplicate"])
        self.assertTrue(self.store.add_transport_event(event)["duplicate"])
        self.assertEqual(len(self.store.transport_events()), 1)
        with self.assertRaises(OrderError): self.store.add_transport_event({**event, "delay_minutes": 60})


if __name__ == "__main__":
    unittest.main()
