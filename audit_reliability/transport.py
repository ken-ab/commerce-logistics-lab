"""Keep verified Qwen audit connections open and record failure stages.

The transport sends each HTTP request once. Only TCP/TLS establishment, before
any HTTP bytes, may try another current DNS address. Original audit-level retry
limits and the single global cost ledger are unchanged.
"""
from datetime import datetime, timezone
from http.client import HTTPSConnection, HTTPException
from io import BytesIO
import hashlib
import json
from pathlib import Path
import socket
import ssl
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
import uuid

from research.model_client import BudgetedChatClient, ModelCallError


HOST = 'dashscope.aliyuncs.com'
PATH = '/compatible-mode/v1/chat/completions'
VERSION = 'audit-connection-reuse-1'


class AuditConnection(HTTPSConnection):
    def __init__(self, event):
        context = ssl.create_default_context()
        context.set_ecdh_curve('prime256v1')
        super().__init__(HOST, port=443, timeout=90, context=context)
        self.event = event

    def connect(self):
        addresses = list(dict.fromkeys(x[4][0] for x in
            socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)))[:8]
        started = time.monotonic()
        last = None
        # Bounded second pass handles the independently observed transient TLS
        # failures. It does not retry a submitted model request.
        for repeat in (1, 2):
            for address in addresses:
                remaining = 45 - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError('TLS establishment deadline')
                raw = None
                phase = 'tcp_connect'
                attempt = time.monotonic()
                try:
                    raw = socket.create_connection((address, 443), timeout=min(3, remaining))
                    phase = 'tls_handshake'
                    raw.settimeout(min(8, max(.01, 45-(time.monotonic()-started))))
                    self.sock = self._context.wrap_socket(raw, server_hostname=HOST)
                    self.sock.settimeout(90)
                    self.event({'phase':phase,'round':repeat,'address':address,'status':'connected',
                                'seconds':round(time.monotonic()-attempt,4)})
                    return
                except ssl.SSLCertVerificationError:
                    if raw is not None:
                        raw.close()
                    self.event({'phase':phase,'round':repeat,'address':address,'status':'certificate_rejected'})
                    raise
                except OSError as error:
                    if raw is not None:
                        raw.close()
                    last = error
                    self.event({'phase':phase,'round':repeat,'address':address,'status':'failed',
                                'error_type':type(error).__name__,'seconds':round(time.monotonic()-attempt,4)})
        raise last or OSError('No current provider DNS addresses')


class AuditResponse:
    def __init__(self, owner, connection, response, trace, started):
        self.owner, self.connection, self.response = owner, connection, response
        self.trace, self.started = trace, started
        self.headers = response.headers
        self.closed = False

    def __enter__(self):
        return self

    def __iter__(self):
        while True:
            line = self.read_line()
            if not line:
                return
            yield line

    def read_line(self):
        return self._read(self.response.readline)

    def read(self, amount=-1):
        return self._read(lambda:self.response.read() if amount < 0 else self.response.read(amount))

    def _read(self, operation):
        self.trace['phase'] = 'response_body'
        try:
            value = operation()
        except (OSError, HTTPException) as error:
            raise self.owner.failure(error, self.trace) from None
        if value and 'first_body_seconds' not in self.trace:
            self.trace['first_body_seconds'] = round(time.monotonic()-self.started,4)
        self.trace['body_bytes_received'] += len(value)
        return value

    def __exit__(self, kind, value, traceback):
        if self.closed:
            return False
        self.closed = True
        reusable = kind is None and not self.response.will_close
        if reusable:
            try:
                # The model parser stops on SSE [DONE]. Drain only the small
                # framing tail before reusing HTTP/1.1; failure simply closes
                # the connection, without repeating the successful generation.
                if self.connection.sock:
                    self.connection.sock.settimeout(2)
                tail = self.response.read(4096)
                self.trace['body_bytes_received'] += len(tail)
                reusable = len(tail) < 4096 and self.response.isclosed()
            except (OSError, HTTPException):
                reusable = False
            finally:
                if self.connection.sock:
                    self.connection.sock.settimeout(90)
        if not reusable:
            self.connection.close()
        self.response.close()
        self.trace.update(status='http_complete' if kind is None else 'failed',
                          connection_reusable=reusable,
                          elapsed_seconds=round(time.monotonic()-self.started,4))
        self.owner.local.last_used = time.monotonic()
        self.owner.save(self.trace)
        return False


class VerifiedAuditTransport:
    def __init__(self, directory: Path, *, connection_factory=AuditConnection):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.factory = connection_factory
        self.local = threading.local()
        self.lock = threading.Lock()
        self.connections = set()

    def save(self, trace):
        path = self.directory/(trace['trace_id']+'.json')
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(trace,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        tmp.replace(path)

    def failure(self, error, trace):
        trace.update(status='failed',error_type=type(error).__name__)
        self.save(trace)
        retryable = isinstance(error,(OSError,HTTPException)) and not isinstance(error,ssl.SSLCertVerificationError)
        return ModelCallError(f"Audit transport {trace['trace_id']} failed in {trace['phase']} "
            f"({type(error).__name__}); HTTP may have been sent={trace['http_may_have_been_sent']}",
            retryable=retryable)

    def __call__(self, request):
        url = urlsplit(request.full_url)
        if (url.scheme!='https' or url.hostname!=HOST or url.port not in (None,443)
                or url.path!=PATH or url.query or url.fragment or url.username or url.password
                or request.get_method()!='POST'):
            raise ModelCallError('Audit transport requires the already-authorized Qwen endpoint')
        if not isinstance(request.data,bytes) or len(request.data)>96000:
            raise ModelCallError('Invalid bounded audit request')
        if json.loads(request.data).get('model')!='qwen3.8-max':
            raise ModelCallError('Audit transport is limited to the existing Qwen reviewer')
        trace = {'version':VERSION,'trace_id':uuid.uuid4().hex,
            'started_at':datetime.now(timezone.utc).isoformat(),'hostname':HOST,
            'request_sha256':hashlib.sha256(request.data).hexdigest(),
            'phase':'connection_establishment','status':'started','http_may_have_been_sent':False,
            'body_bytes_received':0,'certificate_verification':True,'events':[]}
        self.local.last_trace_id = trace['trace_id']
        started = time.monotonic()
        self.save(trace)
        connection = getattr(self.local,'connection',None)
        if connection and time.monotonic()-getattr(self.local,'last_used',0)>30:
            connection.close()
        if connection is None:
            connection = self.factory(trace['events'].append)
            self.local.connection = connection
            with self.lock:
                self.connections.add(connection)
        connection.event = trace['events'].append
        trace['reused_connection'] = connection.sock is not None
        try:
            if connection.sock is None:
                connection.connect()
            trace['connected_seconds'] = round(time.monotonic()-started,4)
            trace['phase'] = 'http_send'
            trace['http_may_have_been_sent'] = True
            connection.request('POST',PATH,body=request.data,headers=dict(request.header_items()))
            trace['phase'] = 'response_headers'
            response = connection.getresponse()
            trace.update(http_status=response.status,headers_seconds=round(time.monotonic()-started,4))
            if response.status!=200:
                trace['phase'] = 'http_error_body'
                try:
                    body = response.read(4096)
                finally:
                    connection.close()
                    response.close()
                trace.update(status='http_error',elapsed_seconds=round(time.monotonic()-started,4))
                self.save(trace)
                raise HTTPError(request.full_url,response.status,'Audit endpoint HTTP error',response.headers,BytesIO(body))
            return AuditResponse(self,connection,response,trace,started)
        except HTTPError:
            raise
        except (OSError,HTTPException) as error:
            connection.close()
            trace['elapsed_seconds'] = round(time.monotonic()-started,4)
            raise self.failure(error,trace) from None

    def close(self):
        with self.lock:
            for connection in self.connections:
                connection.close()


class ObservedAuditClient(BudgetedChatClient):
    def chat(self,*args,**kwargs):
        result = super().chat(*args,**kwargs)
        result['audit_transport_version'] = VERSION
        result['audit_transport_trace_id'] = self.transport.local.last_trace_id
        return result
