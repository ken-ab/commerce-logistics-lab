import socket
import ssl
from types import SimpleNamespace
import pytest

from research.tls_transport import ProviderHTTPSConnection
from research.tls_transport import ProviderHTTPSHandler
from urllib.request import Request


class SocketFixture:
    closed=False
    def close(self): self.closed=True
    def settimeout(self,value): self.timeout=value


def prepare(monkeypatch, failure):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[
        (socket.AF_INET,socket.SOCK_STREAM,6,'',('192.0.2.1',443)),
        (socket.AF_INET,socket.SOCK_STREAM,6,'',('192.0.2.2',443))])
    sockets=[]; hosts=[]
    def create(*args,**kwargs):
        result=SocketFixture(); sockets.append(result); return result
    monkeypatch.setattr(socket,'create_connection',create)
    def wrap(raw,*,server_hostname):
        hosts.append(server_hostname)
        if len(hosts)==1: raise failure
        return raw
    connection=ProviderHTTPSConnection('dashscope.aliyuncs.com',timeout=90)
    connection._context=SimpleNamespace(wrap_socket=wrap)
    return connection,sockets,hosts


def test_tls_timeout_can_try_another_dns_address_before_any_http(monkeypatch):
    connection,sockets,hosts=prepare(monkeypatch,TimeoutError('handshake'))
    connection.connect()
    assert hosts==['dashscope.aliyuncs.com']*2
    assert sockets[0].closed and connection.sock is sockets[1]
    assert connection.sock.timeout==90


def test_certificate_failure_is_not_bypassed_by_address_failover(monkeypatch):
    connection,sockets,hosts=prepare(monkeypatch,ssl.SSLCertVerificationError('invalid certificate'))
    with pytest.raises(ssl.SSLCertVerificationError): connection.connect()
    assert len(hosts)==1 and sockets[0].closed


def test_handler_uses_runtime_connection_signature_and_certificate_checks(monkeypatch):
    context=ssl.create_default_context()
    handler=ProviderHTTPSHandler(context=context)
    def opening(factory,request,**kwargs):
        connection=factory(request.host,**kwargs)
        assert connection._context.check_hostname
        assert connection._context.verify_mode==ssl.CERT_REQUIRED
        return 'valid'
    monkeypatch.setattr(handler,'do_open',opening)
    assert handler.https_open(Request('https://dashscope.aliyuncs.com/'))=='valid'
