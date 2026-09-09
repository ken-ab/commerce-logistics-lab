from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from research.budget import BudgetExceeded, BudgetLedger


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy_path = self.root / "policy.json"
        self.policy = {"currency": "CNY", "total_limit": 300, "automatic_spend_ceiling": 1,
                       "maximum_per_call_cny": 1, "state": "ready"}
        self.save()
        self.ledger = BudgetLedger(self.root / "ledger.sqlite", self.policy_path)

    def save(self):
        self.policy_path.write_text(json.dumps(self.policy), encoding="utf-8")

    def reserve(self, maximum="0.6"):
        return self.ledger.reserve(maximum_cny=maximum, model="test-model", purpose="unit-test", price_version="test")

    def test_parallel_reservations_cannot_overspend(self):
        def attempt():
            try:
                self.reserve()
                return True
            except BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(lambda _: attempt(), range(8)))
        self.assertEqual(sum(accepted), 1)
        self.assertEqual(self.ledger.summary()["accounted_and_reserved_cny"], "0.6")

    def test_uncertain_calls_remain_charged_across_restart(self):
        call_id = self.reserve()
        self.ledger.mark_uncertain(call_id)
        resumed = BudgetLedger(self.root / "ledger.sqlite", self.policy_path)
        with self.assertRaises(BudgetExceeded):
            resumed.reserve(maximum_cny="0.6", model="test", purpose="retry", price_version="test")
        self.assertEqual(resumed.summary()["status_counts"], {"uncertain": 1})

    def test_settlement_releases_only_confirmed_unused_reservation(self):
        call_id = self.reserve()
        self.ledger.settle(call_id, cost_cny="0.1", usage={"input_tokens": 10, "output_tokens": 2})
        self.reserve("0.9")
        with self.assertRaises(BudgetExceeded):
            self.reserve("0.000001")

    def test_unverified_provider_cannot_reserve(self):
        self.policy["state"] = "provider_and_price_verification_required"
        self.save()
        with self.assertRaisesRegex(RuntimeError, "verified"):
            self.reserve()

    def test_underestimate_is_recorded_and_disables_new_calls(self):
        call_id = self.reserve()
        with self.assertRaises(BudgetExceeded):
            self.ledger.settle(call_id, cost_cny="0.7", usage={"output_tokens": 1})
        self.assertEqual(self.ledger.summary()["accounted_and_reserved_cny"], "0.7")
        with self.assertRaises(RuntimeError):
            self.reserve("0.1")


if __name__ == "__main__":
    unittest.main()
