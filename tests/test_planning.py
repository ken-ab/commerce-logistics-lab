from __future__ import annotations

import copy
import json
import unittest

from logistics_lab.planning import ROOT, audit_report, load_world, plan_fulfilment, review_and_replan


class PlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = json.loads((ROOT / "data/scenarios.json").read_text(encoding="utf-8"))
        self.order = self.cases[0]["order"]

    def test_authored_scenarios(self) -> None:
        for case in self.cases:
            with self.subTest(case=case["id"]):
                result = plan_fulfilment(case["order"])
                self.assertEqual(result["status"], case["expected_status"])
                if "expected_cost_usd" in case:
                    self.assertEqual(result["total_cost_usd"], case["expected_cost_usd"])
                    self.assertEqual(result["transit_days"], case["expected_days"])
                    self.assertTrue(audit_report(case["order"], result)["passed"])

    def test_report_tampering_is_detected(self) -> None:
        changes = {"total_cost_usd": 1, "transit_days": 1, "weight_kg": 1, "quantities": {"TEE-BLK-M": 1}, "evidence_ids": [], "dataset_id": "invented", "modes": ["teleport"], "leg_ids": ["NO-SUCH-ROUTE"], "warehouse": "HK-HUB"}
        for key, value in changes.items():
            with self.subTest(field=key):
                plan = plan_fulfilment(self.order)
                plan[key] = value
                self.assertFalse(audit_report(self.order, plan)["passed"])

    def test_disconnected_and_looped_routes_are_rejected(self) -> None:
        for ids in (["HK-EU-AIR", "SZ-HK-ROAD", "EU-DE-ROAD"], ["SZ-HK-ROAD", "SZ-HK-ROAD", "HK-EU-AIR", "EU-DE-ROAD"]):
            plan = plan_fulfilment(self.order)
            plan["leg_ids"] = ids
            self.assertFalse(audit_report(self.order, plan)["passed"])

    def test_disruption_invalidates_a_previously_valid_plan(self) -> None:
        plan = plan_fulfilment(self.order)
        changed = {**self.order, "blocked_legs": ["HK-EU-AIR"]}
        self.assertFalse(audit_report(changed, plan)["passed"])
        self.assertEqual(plan_fulfilment(changed)["status"], "infeasible")

    def test_duplicate_sku_lines_cannot_bypass_stock(self) -> None:
        order = {**self.order, "items": [{"sku": "TEE-BLK-M", "quantity": 100}] * 2}
        self.assertEqual(plan_fulfilment(order)["status"], "infeasible")

    def test_invalid_numbers_need_clarification(self) -> None:
        for value in (-1, float("nan"), float("inf"), True, "300"):
            self.assertEqual(plan_fulfilment({**self.order, "budget_usd": value})["status"], "needs_clarification")

    def test_empty_output_does_not_receive_a_perfect_score(self) -> None:
        result = audit_report(self.order, {})
        self.assertFalse(result["assessed"])
        self.assertFalse(result["passed"])

    def test_review_is_bounded_and_fixes_the_demonstration_deadline(self) -> None:
        result = review_and_replan(self.order)
        self.assertFalse(result["initial_audit"]["passed"])
        self.assertEqual(result["reruns"], 1)
        self.assertTrue(result["final_audit"]["passed"])
        self.assertEqual(result["final"]["transit_days"], 6)

    def test_reads_do_not_reserve_or_modify_stock(self) -> None:
        world = load_world()
        before = copy.deepcopy(world)
        plan_fulfilment(self.order, world=world)
        self.assertEqual(world, before)


if __name__ == "__main__":
    unittest.main()
