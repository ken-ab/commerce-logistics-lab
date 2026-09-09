"""Verified TLS establishment across a provider's current DNS addresses.

Only pre-HTTP connection establishment can fail over. Once TLS succeeds the
caller sends one request; no HTTP request or model generation is retried here.
"""
from http.client import HTTPSConnection
import socket
import ssl
from urllib.request import HTTPSHandler


class ProviderHTTPSConnection(HTTPSConnection):
    def connect(self):
        if self.host!='dashscope.aliyuncs.com' or self._tunnel_host:
            return super().connect()
        addresses=list(dict.fromkeys(item[4][0] for item in
            socket.getaddrinfo(self.host,self.port,type=socket.SOCK_STREAM)))[:8]
        last_error=None
        for address in addresses:
            raw=None
            try:
                raw=socket.create_connection((address,self.port),
                    timeout=min(self.timeout,8),source_address=self.source_address)
                # The certificate is always checked against the provider hostname,
                # never against the selected address. No HTTP bytes have been sent.
                self.sock=self._context.wrap_socket(raw,server_hostname=self.host)
                self.sock.settimeout(self.timeout)
                return
            except ssl.SSLCertVerificationError:
                if raw is not None:
                    raw.close()
                raise
            except OSError as error:
                if raw is not None:
                    raw.close()
                last_error=error
        raise last_error or OSError('Provider DNS returned no usable addresses')


class ProviderHTTPSHandler(HTTPSHandler):
    def https_open(self,request):
        return self.do_open(ProviderHTTPSConnection,request,context=self._context)
