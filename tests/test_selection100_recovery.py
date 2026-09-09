"""No network: verify repaired wire parameters and retained accounting evidence."""
from contextlib import closing
import io
import json
from pathlib import Path
import tempfile
import unittest

from model_selection_100.configured import ConfiguredClient
from model_selection_100.recovery_client import RecoveryClient, REPAIRED_MODEL
from research.budget import BudgetExceeded


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        models = [{'id': mid, 'reasoning_effort': 'none',
                   'pricing_usd_per_million': {'input': .95, 'output': 3.9995}}
                  for mid in (REPAIRED_MODEL, 'fixture-other')]
        out = self.root / 'evidence/model_selection_100'
        out.mkdir(parents=True)
        for name in ('candidates.json', 'execution_models.json'):
            (out / name).write_text(json.dumps({'models': models}))
        policy = self.root / 'model_selection_100/budget_policy.json'
        policy.parent.mkdir()
        policy.write_text(json.dumps({'currency': 'CNY', 'total_limit': 480,
            'automatic_spend_ceiling': 480, 'selection_task_limit_cny': 100,
            'maximum_per_call_cny': 20, 'state': 'ready'}))
        self.config = {'AIHUBMIX_BASE_URL': 'https://aihubmix.com/v1', 'AIHUBMIX_API_KEY': 'fixture-only'}
        self.requests = []

    def client(self, completion=8, finish='length', cls=RecoveryClient):
        def transport(request):
            body = json.loads(request.data)
            self.requests.append(body)
            response = io.StringIO(json.dumps({'model': body['model'],
                'choices': [{'message': {'content': '{"order":["a"]}'}, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': completion,
                          'completion_tokens_details': {'reasoning_tokens': max(0, completion - 2)}}}))
            response.headers = {'Content-Type': 'application/json'}
            return response
        return cls(root=self.root, config=self.config, transport=transport)

    def call(self, client, mid=REPAIRED_MODEL, suffix='one', **kwargs):
        return client.chat(mid, {'query': 'fixture'}, purpose='model-selection-100:calibration-cap:' + suffix,
                           reservation_record=self.root / (suffix + '.json'), **kwargs)

    def test_correct_wire_cap_and_native_reservation(self):
        client = self.client()
        result = self.call(client, max_output_tokens=8)
        self.assertEqual(self.requests[0]['max_tokens'], 8)
        self.assertNotIn('max_completion_tokens', self.requests[0])
        self.assertEqual(result['reserved_output_tokens'], 32768)
        self.assertTrue(result['output_cap_honored'])
        with closing(client.ledger.connect()) as db:
            reserved, charged = db.execute('SELECT reserved,charged FROM calls').fetchone()
        self.assertGreater(reserved, 32768 * 3.9995 * 8)
        self.assertLess(charged, reserved)

    def test_an_ignored_cap_halts_future_calls_and_retains_usage(self):
        client = self.client(completion=5650, finish='stop')
        result = self.call(client)
        self.assertEqual(result['status'], 'output_limit_not_enforced')
        self.assertEqual(result['usage']['completion_tokens'], 5650)
        self.assertAlmostEqual(float(result['estimated_cost_cny']), (100 * .95 + 5650 * 3.9995) * 8 / 1e6)
        with self.assertRaises(BudgetExceeded):
            self.call(client, suffix='two')

    def test_cost_overrun_does_not_erase_measured_tokens(self):
        client = self.client(completion=70000, finish='stop')
        result = self.call(client)
        self.assertEqual(result['error_type'], 'BudgetExceeded')
        self.assertEqual(result['usage']['completion_tokens'], 70000)
        self.assertIsNotNone(result['estimated_cost_cny'])
        self.assertEqual(client.ledger.summary()['halt_reason'], 'cost_bound_requires_review')

    def test_other_models_keep_the_same_request(self):
        self.call(self.client(20, 'stop', ConfiguredClient), mid='fixture-other', suffix='original')
        self.call(self.client(20, 'stop'), mid='fixture-other', suffix='recovery')
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertEqual(self.requests[1]['max_completion_tokens'], 1024)

    def test_changed_cap_is_not_allowed_in_a_formal_case(self):
        with self.assertRaises(ValueError):
            self.client().chat(REPAIRED_MODEL, {}, purpose='model-selection-100:screen:any',
                               reservation_record=self.root / 'bad.json', max_output_tokens=8)
        self.assertFalse(self.requests)

    def test_calibration_cannot_repeat_a_paid_request(self):
        client = self.client()
        self.call(client, max_output_tokens=8)
        with self.assertRaises(BudgetExceeded):
            self.call(client, max_output_tokens=8)
        self.assertEqual(len(self.requests), 1)
