from copy import deepcopy
from datetime import datetime, timezone, timedelta
import unittest

from apparel_fulfillment.orders import check_order, OrderError
from apparel_fulfillment.transport import instant, load_corridor, plan_transport, validate_event
from apparel_fulfillment.route_audit import audit_route
from test_apparel_orders import fixture, request

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
SHIP = {"destination": "DE-DC", "ready_at": "2026-09-08T08:00:00+08:00", "deadline_at": "2026-09-12T00:00:00Z", "budget_cents": 20000}


class ApparelTransportTests(unittest.TestCase):
    def setUp(self):
        self.world = fixture(); self.corridor = load_corridor()
        for v in self.world["variants"].values():
            v["provenance"]["weight_grams_per_catalog_unit"] = {"kind": "simulation", "evidence_id": "test-weight"}
        req = request(); req["needs_shipping"] = True
        self.checked = check_order(req, [{"line_id": "one", "sku": "A"}], self.world)

    def plan(self, shipping=SHIP, events=(), now=NOW):
        return plan_transport(self.checked, shipping, self.world, corridor=self.corridor, events=events, now=now)

    def audit(self, plan, shipping=SHIP, events=(), now=NOW):
        return audit_route(self.checked, shipping, plan, self.world, self.corridor, events=events, now=now)

    def cancellation(self, plan):
        segment = next(s for s in plan["segments"] if s["mode"] == "air")
        return {"event_id": "cancel-1", "kind": "cancel", "leg_id": segment["leg_id"], "nominal_departure": segment["nominal_departure"], "published_at": "2026-09-08T00:00:00Z"}

    def test_normal_route_includes_schedule_waits_and_independent_audit(self):
        plan = self.plan()
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["total_cost_cents"], 17300)
        self.assertEqual([s["mode"] for s in plan["segments"]], ["road", "air", "road"])
        self.assertEqual(plan["arrival_at"], "2026-09-10T20:00:00Z")
        self.assertTrue(self.audit(plan)["passed"])

    def test_cancellation_invalidates_old_route_and_replanning_uses_other_service(self):
        old = self.plan(); event = self.cancellation(old)
        self.assertIn("cancelled_departure", self.audit(old, events=[event])["violations"])
        revised = self.plan(events=[event])
        self.assertNotEqual(old["segments"][1]["service_id"], revised["segments"][1]["service_id"])
        self.assertTrue(self.audit(revised, events=[event])["passed"])

    def test_delay_can_make_later_scheduled_departure_better(self):
        old = self.plan(); event = self.cancellation(old)
        event.update(kind="delay", delay_minutes=1440)
        new = self.plan(events=[event, event])
        self.assertNotEqual(old["segments"][1]["service_id"], new["segments"][1]["service_id"])
        self.assertTrue(self.audit(new, events=[event])["passed"])

    def test_unrelated_and_not_yet_published_events_do_not_invalidate(self):
        old = self.plan(); event = self.cancellation(old)
        event["nominal_departure"] = "2026-09-11T16:00:00Z"
        self.assertTrue(self.audit(old, events=[event])["passed"])
        future = self.cancellation(old); future["published_at"] = "2026-09-09T00:00:00Z"
        self.assertEqual(self.plan(events=[future])["segments"], old["segments"])

    def test_budget_and_deadline_relaxations_are_options_not_applied(self):
        ship = {**SHIP, "budget_cents": 16000}
        plan = self.plan(ship)
        self.assertEqual(plan["status"], "infeasible")
        self.assertTrue(any(o.get("minimum_shipping_budget_cents") == 17300 for o in plan["adjustment_options"]))
        self.assertEqual(plan["shipping_constraints"]["budget_cents"], 16000)

    def test_confirmation_after_first_departure_is_rejected(self):
        plan = self.plan()
        self.assertIn("missed_departure_or_transfer", self.audit(plan, now=NOW + timedelta(seconds=1))["violations"])

    def test_forged_cost_or_time_or_segment_is_rejected(self):
        plan = self.plan()
        for mutation in ("total", "arrival", "leg"):
            changed = deepcopy(plan)
            if mutation == "total": changed["total_cost_cents"] = 1
            elif mutation == "arrival": changed["segments"][0]["arrival_at"] = "2026-09-08T00:01:00Z"
            else: changed["segments"][1]["leg_id"] = "UNKNOWN"
            self.assertFalse(self.audit(changed)["passed"])

    def test_no_shipping_skips_shipping_fields_and_nonready_order_cannot_plan(self):
        no_ship = {**self.checked, "needs_shipping": False}
        self.assertEqual(plan_transport(no_ship, None, self.world)["status"], "not_required")
        blocked = {**self.checked, "status": "unfulfillable"}
        self.assertEqual(plan_transport(blocked, SHIP, self.world)["status"], "order_not_ready")

    def test_invalid_event_and_naive_time_rejected(self):
        event = self.cancellation(self.plan()); event["nominal_departure"] = "2026-09-08T16:01:00Z"
        with self.assertRaises(OrderError): validate_event(event, self.corridor)
        with self.assertRaises(OrderError): instant("2026-09-08T12:00:00")


if __name__ == "__main__":
    unittest.main()
