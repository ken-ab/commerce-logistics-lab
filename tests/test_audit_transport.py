from datetime import date
from io import BytesIO
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

from audit_reliability.transport import AuditConnection, HOST, PATH, ObservedAuditClient, VerifiedAuditTransport
from research.model_client import ModelCallError, parse_response


def stream():
    frames = [
        {'model': 'qwen3.8-max', 'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]},
        {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}},
    ]
    return (''.join('data: '+json.dumps(f)+'\n\n' for f in frames)+'data: [DONE]\n\n').encode()


class FakeResponse:
    def __init__(self, body=None, status=200, fail_read=False):
        self.fp = BytesIO(stream() if body is None else body)
        self.status, self.fail_read = status, fail_read
        self.headers = {'Content-Type': 'text/event-stream'}
        self.will_close = False
        self.closed = False

    def read(self, amount=-1):
        if self.fail_read:
            raise TimeoutError('private provider detail')
        return self.fp.read(amount)

    def readline(self):
        if self.fail_read:
            raise TimeoutError('private provider detail')
        return self.fp.readline()

    def isclosed(self):
        return self.fp.tell() == len(self.fp.getvalue())

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, event, *, response=None, header_error=None):
        self.event, self.sock = event, None
        self.requests, self.connects, self.closes = [], 0, 0
        self.response, self.header_error = response, header_error

    def connect(self):
        self.connects += 1
        self.sock = Mock()

    def request(self, method, path, *, body, headers):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        if self.header_error:
            raise self.header_error
        return self.response or FakeResponse()

    def close(self):
        self.closes += 1
        self.sock = None


class AuditTransportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root/'research').mkdir()
        (self.root/'research/budget_policy.json').write_text(json.dumps({
            'currency':'CNY','state':'ready','total_limit':300,'automatic_spend_ceiling':1}))
        (self.root/'research/rate_card.json').write_text(json.dumps({
            'currency':'CNY','verified_on':date.today().isoformat(),'version':'fixture',
            'models':{'qwen3.8-max':{'provider':'dashscope','endpoint_host':HOST,
                'input_per_million':'12','output_per_million':'36'}}}))
        self.config = {'DASHSCOPE_API_KEY':'fixture-secret-not-real',
            'DASHSCOPE_TEXT_BASE_URL':'https://'+HOST+'/compatible-mode/v1',
            'COMMERCE_MODEL':'qwen3.8-max'}

    def transport(self, **options):
        self.connections = []
        def factory(event):
            result = FakeConnection(event, **options)
            self.connections.append(result)
            return result
        result = VerifiedAuditTransport(self.root/'traces',connection_factory=factory)
        self.addCleanup(result.close)
        return result

    def request(self, url=None, model='qwen3.8-max'):
        return Request(url or 'https://'+HOST+PATH,method='POST',
            data=json.dumps({'model':model,'messages':[{'content':'private fixture prompt'}]}).encode(),
            headers={'Authorization':'Bearer fixture-secret-not-real'})

    def traces(self):
        return [json.loads(p.read_text()) for p in (self.root/'traces').glob('*.json')]

    def test_exact_payload_once_and_reuses_verified_connection(self):
        transport = self.transport()
        request = self.request()
        for _ in range(2):
            with transport(request) as response:
                self.assertEqual(parse_response(response)['usage']['completion_tokens'],2)
        connection = self.connections[0]
        self.assertEqual(len(self.connections),1)
        self.assertEqual(connection.connects,1)
        self.assertEqual(len(connection.requests),2)
        self.assertTrue(all(r[2] == request.data for r in connection.requests))
        traces = self.traces()
        self.assertEqual(sorted(t['reused_connection'] for t in traces),[False,True])
        self.assertTrue(all(t['connection_reusable'] for t in traces))
        text = json.dumps(traces)
        self.assertNotIn('fixture-secret',text)
        self.assertNotIn('private fixture prompt',text)

    def test_header_timeout_is_not_reposted_and_retains_full_reservation(self):
        transport = self.transport(header_error=TimeoutError('private provider detail'))
        client = ObservedAuditClient(root=self.root,config=self.config,transport=transport)
        with self.assertRaises(ModelCallError) as error:
            client.chat([{'role':'user','content':'fixture'}],purpose='offline-transport-test')
        self.assertEqual(len(self.connections[0].requests),1)
        self.assertIsNone(self.connections[0].sock)
        self.assertEqual(client.ledger.summary()['status_counts'],{'uncertain':1})
        self.assertIn('response_headers',str(error.exception))
        self.assertNotIn('private provider detail',str(error.exception))
        self.assertTrue(self.traces()[0]['http_may_have_been_sent'])

    def test_stream_timeout_closes_connection_without_reposting(self):
        transport = self.transport(response=FakeResponse(fail_read=True))
        with self.assertRaises(ModelCallError):
            with transport(self.request()) as response:
                parse_response(response)
        self.assertEqual(len(self.connections[0].requests),1)
        self.assertIsNone(self.connections[0].sock)
        self.assertEqual(self.traces()[0]['phase'],'response_body')
        self.assertEqual(self.traces()[0]['status'],'failed')

    def test_incomplete_stream_is_not_reused(self):
        transport = self.transport(response=FakeResponse(body=b'data: {"choices": []}\n\n'))
        with self.assertRaises(ModelCallError):
            with transport(self.request()) as response:
                parse_response(response)
        self.assertFalse(self.traces()[0]['connection_reusable'])
        self.assertEqual(len(self.connections[0].requests),1)

    def test_endpoint_and_model_validation_happens_before_connect(self):
        transport = self.transport()
        for url,model in [('https://example.com'+PATH,'qwen3.8-max'),
                          ('https://'+HOST+PATH+'?redirect=1','qwen3.8-max'),
                          (None,'other-model')]:
            with self.subTest(url=url,model=model),self.assertRaises(ModelCallError):
                transport(self.request(url,model))
        self.assertEqual(self.connections,[])

    def test_redirect_is_never_followed_and_error_response_is_closed(self):
        raw = FakeResponse(body=b'{}',status=302)
        transport = self.transport(response=raw)
        with self.assertRaises(HTTPError) as error:
            transport(self.request())
        self.assertEqual(error.exception.code,302)
        self.assertEqual(len(self.connections[0].requests),1)
        self.assertTrue(raw.closed)
        self.assertIsNone(self.connections[0].sock)

    def test_success_settles_usage_and_links_trace(self):
        transport = self.transport()
        client = ObservedAuditClient(root=self.root,config=self.config,transport=transport)
        result = client.chat([{'role':'user','content':'fixture'}],purpose='offline-transport-test')
        self.assertEqual(result['estimated_cost_cny'],'0.000192')
        self.assertEqual(client.ledger.summary()['status_counts'],{'settled':1})
        self.assertEqual(result['audit_transport_trace_id'],self.traces()[0]['trace_id'])

    def test_certificate_failure_stops_before_address_fallback(self):
        events = []
        connection = AuditConnection(events.append)
        self.addCleanup(connection.close)
        context = Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError('fixture')
        connection._context = context
        raw = Mock()
        addresses = [(2,1,6,'',('192.0.2.1',443)),(2,1,6,'',('192.0.2.2',443))]
        with patch('audit_reliability.transport.socket.getaddrinfo',return_value=addresses), \
             patch('audit_reliability.transport.socket.create_connection',return_value=raw) as tcp:
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.connect()
        self.assertEqual(tcp.call_count,1)
        raw.close.assert_called_once()
        self.assertEqual(events[0]['status'],'certificate_rejected')

    def test_transient_tls_failure_can_try_next_address_before_http(self):
        events = []
        connection = AuditConnection(events.append)
        self.addCleanup(connection.close)
        context = Mock()
        tls = Mock()
        context.wrap_socket.side_effect = [TimeoutError('fixture'),tls]
        connection._context = context
        first,second = Mock(),Mock()
        addresses = [(2,1,6,'',('192.0.2.1',443)),(2,1,6,'',('192.0.2.2',443))]
        with patch('audit_reliability.transport.socket.getaddrinfo',return_value=addresses), \
             patch('audit_reliability.transport.socket.create_connection',side_effect=[first,second]) as tcp:
            connection.connect()
        self.assertEqual(tcp.call_count,2)
        first.close.assert_called_once()
        self.assertEqual([e['status'] for e in events],['failed','connected'])
        self.assertIs(connection.sock,tls)


if __name__ == '__main__':
    unittest.main()
