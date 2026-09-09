from datetime import date
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from research.model_client import BudgetedChatClient, ModelCallError, parse_response


class Response(BytesIO):
    headers = {'Content-Type': 'application/json'}


class ModelClientTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'research').mkdir()
        self.policy = {'currency': 'CNY', 'state': 'ready', 'total_limit': 300, 'automatic_spend_ceiling': 1}
        (self.root / 'research/budget_policy.json').write_text(json.dumps(self.policy))
        card = {'currency': 'CNY', 'verified_on': date.today().isoformat(), 'version': 'test', 'models': {
            'test': {'provider': 'dashscope', 'endpoint_host': 'dashscope.aliyuncs.com',
                     'input_per_million': '12', 'output_per_million': '36'}}}
        (self.root / 'research/rate_card.json').write_text(json.dumps(card))
        self.config = {'DASHSCOPE_API_KEY': 'fixture-not-real', 'DASHSCOPE_TEXT_BASE_URL':
            'https://dashscope.aliyuncs.com/compatible-mode/v1', 'COMMERCE_MODEL': 'test'}

    def test_reserves_before_transport_and_counts_reasoning_once(self):
        def transport(request):
            self.assertEqual(client.ledger.summary()['status_counts'], {'reserved': 1})
            body = json.loads(request.data)
            self.assertEqual(body['max_completion_tokens'], 512)
            self.assertNotIn('max_tokens', body)
            return Response(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20,
                          'completion_tokens_details': {'reasoning_tokens': 10}}}).encode())
        client = BudgetedChatClient(root=self.root, config=self.config, transport=transport)
        result = client.chat([{'role': 'user', 'content': 'test'}], purpose='test', max_completion_tokens=512)
        self.assertEqual(result['estimated_cost_cny'], '0.00192')
        self.assertEqual(client.ledger.summary()['status_counts'], {'settled': 1})

    def test_timeout_has_no_hidden_retry_and_keeps_reservation(self):
        attempts = []
        def transport(request):
            attempts.append(1)
            raise TimeoutError('must not echo arbitrary transport detail')
        client = BudgetedChatClient(root=self.root, config=self.config, transport=transport)
        with self.assertRaises(ModelCallError) as caught:
            client.chat([{'role': 'user', 'content': 'test'}], purpose='test')
        self.assertEqual(len(attempts), 1)
        self.assertNotIn('arbitrary', str(caught.exception))
        self.assertEqual(client.ledger.summary()['status_counts'], {'uncertain': 1})

    def test_unpriced_model_fails_before_spending(self):
        client = BudgetedChatClient(root=self.root, config=self.config,
                                   transport=lambda _: self.fail('No request allowed'))
        with self.assertRaises(ModelCallError):
            client.chat([{'role': 'user', 'content': 'test'}], purpose='test', model='unpriced')
        self.assertEqual(client.ledger.summary()['calls'], 0)

    def test_dashscope_forced_audit_uses_supported_nonthinking_mode(self):
        def transport(request):
            body=json.loads(request.data)
            self.assertIs(body['enable_thinking'],False)
            self.assertNotIn('thinking_budget',body)
            self.assertNotIn('preserve_thinking',body)
            self.assertEqual(body['tool_choice']['function']['name'],'audit')
            return Response(json.dumps({'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':10,'completion_tokens':2}}).encode())
        client=BudgetedChatClient(root=self.root,config=self.config,transport=transport)
        client.chat([{'role':'user','content':'test'}],purpose='test',max_completion_tokens=512,
            tools=[{'type':'function','function':{'name':'audit','parameters':{'type':'object'}}}],
            tool_choice={'type':'function','function':{'name':'audit'}})

    def test_stream_fragments_reconstruct_tools_and_usage(self):
        frames = [
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'a', 'function': {'name': 'lookup', 'arguments': '{"sku":'}}]}}]},
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '"x"}'}}]}, 'finish_reason': 'tool_calls'}]},
            {'choices': [], 'usage': {'prompt_tokens': 20, 'completion_tokens': 10}}]
        response = Response((''.join('data: ' + json.dumps(f) + '\n\n' for f in frames) + 'data: [DONE]\n').encode())
        response.headers = {'Content-Type': 'text/event-stream'}
        result = parse_response(response)
        self.assertEqual(json.loads(result['choices'][0]['message']['tool_calls'][0]['function']['arguments']), {'sku': 'x'})
        self.assertEqual(result['usage']['completion_tokens'], 10)


if __name__ == '__main__':
    unittest.main()
