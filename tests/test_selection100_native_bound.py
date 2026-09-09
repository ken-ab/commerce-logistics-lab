"""Cost-only revision retains HTTP bytes and accounts for mandatory thinking."""
from contextlib import closing
from decimal import Decimal
import unittest

from test_selection100_recovery import RecoveryTests
from model_selection_100.configured import ConfiguredClient
from model_selection_100.native_bound_client import NativeBoundClient, REPAIRED_MODEL
from research.budget import BudgetExceeded


class NativeBoundTests(unittest.TestCase):
    setUp = RecoveryTests.setUp
    client = RecoveryTests.client
    call = RecoveryTests.call

    def test_kimi_request_is_byte_identical_and_measured_thinking_is_preserved(self):
        self.call(self.client(20, 'stop', ConfiguredClient), suffix='original')
        client = self.client(5650, 'stop', NativeBoundClient)
        response = self.call(client, suffix='native')
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertEqual(response['usage']['completion_tokens'], 5650)
        self.assertFalse(response['requested_output_cap_honored'])
        self.assertTrue(response['output_cap_honored'])
        self.assertIsNone(client.ledger.summary()['halt_reason'])
        with closing(client.ledger.connect()) as db:
            reserved, charged = db.execute('SELECT reserved,charged FROM calls WHERE id=?', (response['budget_call_id'],)).fetchone()
        self.assertGreater(reserved, 262144 * 3.9995 * 8)
        self.assertLess(charged, reserved)

    def test_full_context_reservation_cannot_cross_task_cap(self):
        client = self.client(20, 'stop', NativeBoundClient)
        with closing(client.ledger.connect()) as db, db:
            db.execute("INSERT INTO calls VALUES ('prior','now','model-selection-100:old','test','test',95000000,NULL,'uncertain',NULL,NULL)")
        with self.assertRaises(BudgetExceeded):
            self.call(client)
        self.assertEqual(self.requests, [])

    def test_provider_output_beyond_context_keeps_usage_and_stops(self):
        client = self.client(400000, 'stop', NativeBoundClient)
        response = self.call(client)
        self.assertEqual(response['usage']['completion_tokens'], 400000)
        self.assertEqual(client.ledger.summary()['halt_reason'], 'cost_bound_requires_review')

    def test_other_models_still_use_original_wire(self):
        self.call(self.client(20, 'stop', ConfiguredClient), mid='fixture-other', suffix='original')
        self.call(self.client(20, 'stop', NativeBoundClient), mid='fixture-other', suffix='native')
        self.assertEqual(self.requests[0], self.requests[1])
