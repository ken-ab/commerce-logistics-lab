"""Bounded unauthenticated transport probes; no model inference or config edits."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import http.client
import json
import socket
import ssl
import subprocess
import time

HOST = 'dashscope.aliyuncs.com'
PATH = '/compatible-mode/v1/models'
ROOT = Path(__file__).resolve().parents[1]


def python_probe(address, curve):
    started = time.monotonic()
    record = {'client': 'python-openssl', 'curve': curve, 'ip': address,
              'request': 'Unauthenticated GET /compatible-mode/v1/models',
              'certificate_verification': True, 'proxy': 'direct'}
    raw = sock = None
    phase = 'tcp_connect'
    try:
        raw = socket.create_connection((address, 443), timeout=3)
        record['tcp_seconds'] = round(time.monotonic() - started, 4)
        context = ssl.create_default_context()
        if curve == 'P-256':
            context.set_ecdh_curve('prime256v1')
        phase = 'tls_handshake'
        raw.settimeout(6)
        sock = context.wrap_socket(raw, server_hostname=HOST)
        record['tls_seconds'] = round(time.monotonic() - started, 4)
        record['tls_version'] = sock.version()
        phase = 'response_headers'
        sock.settimeout(8)
        sock.sendall((f'GET {PATH} HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n'
                      'User-Agent: local-qwen-connectivity-diagnostic\r\n\r\n').encode('ascii'))
        response = http.client.HTTPResponse(sock)
        response.begin()
        record['http_status'] = response.status
        record['status'] = 'http_response_received'
        record['response_headers_seconds'] = round(time.monotonic() - started, 4)
        response.close()
    except (OSError, http.client.HTTPException) as error:
        record.update(status='failed', phase=phase, error_type=type(error).__name__)
    finally:
        if sock is not None:
            sock.close()
        if raw is not None:
            raw.close()
    record['elapsed_seconds'] = round(time.monotonic() - started, 4)
    return record


def curl_probe(address):
    started = time.monotonic()
    record = {'client': 'curl-schannel', 'ip': address,
              'request': 'Unauthenticated GET /compatible-mode/v1/models',
              'certificate_verification': True, 'proxy': 'direct'}
    command = [r'C:\Windows\System32\curl.exe', '--noproxy', '*', '--connect-timeout', '9',
               '--max-time', '17', '--resolve', f'{HOST}:443:{address}', '-sS', '-o', 'NUL',
               '-w', '%{json}', f'https://{HOST}{PATH}']
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        data = json.loads(result.stdout)
        record.update(status='http_response_received' if data.get('http_code') else 'failed',
                      exit_code=result.returncode,
                      **{k:data.get(k) for k in ['http_code', 'time_connect', 'time_appconnect',
                                               'time_starttransfer', 'time_total', 'ssl_verify_result']})
        if result.returncode:
            record['error'] = result.stderr.strip()[:400]
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        record.update(status='failed', error_type=type(error).__name__)
    record['elapsed_seconds'] = round(time.monotonic() - started, 4)
    return record


def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(
        HOST, 443, type=socket.SOCK_STREAM)))[:4]
    target = ROOT / 'evidence/qwen_connectivity' / stamp
    target.mkdir(parents=True)
    record = {'created_at': datetime.now(timezone.utc).isoformat(), 'hostname': HOST,
              'dns_addresses': addresses, 'openssl': ssl.OPENSSL_VERSION,
              'credentials_used': False, 'model_inferences': 0,
              'config_changes': False,
              'limitations': 'One attempt per TLS stack/address. An unauthenticated HTTP response proves transport only, not model availability. Concurrent evaluation may affect timing.'}
    # At most three diagnostics overlap. Cases are interleaved by address.
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = []
        for address in addresses:
            pending.extend([pool.submit(python_probe, address, 'default'),
                            pool.submit(python_probe, address, 'P-256'),
                            pool.submit(curl_probe, address)])
        record['observations'] = [future.result() for future in pending]
    (target / 'result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'file': str(target / 'result.json'), **record}, ensure_ascii=False))


if __name__ == '__main__':
    main()
