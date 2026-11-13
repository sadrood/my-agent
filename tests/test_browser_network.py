import contextlib
import http.client
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from tools.browser_network import BrowserNetworkProxy, NetworkAccessDenied, NetworkPolicy


def answer(address, port=443):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', sockaddr


@pytest.mark.parametrize('address', [
    '0.0.0.0', '10.0.0.1', '127.0.0.1', '169.254.169.254',
    '172.16.0.1', '192.168.1.1', '100.64.0.1', '100.100.100.200',
    '192.0.2.1', '198.18.0.1', '198.51.100.1', '203.0.113.1',
    '224.0.0.1', '240.0.0.1', '255.255.255.255', '168.63.129.16',
    '::', '::1', 'fe80::1', 'fc00::1', 'ff02::1', '2001:db8::1',
    '::ffff:8.8.8.8', '::ffff:127.0.0.1', '64:ff9b::808:808',
    '64:ff9b:1::1', '2002:0808:0808::1', '2001::1',
])
def test_rejects_dangerous_answers(monkeypatch, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: [answer(address)])
    with pytest.raises(NetworkAccessDenied):
        NetworkPolicy().resolve('public.example', 443)


@pytest.mark.parametrize('addresses', [
    ['8.8.8.8', '127.0.0.1'], ['127.0.0.1', '8.8.8.8'],
    ['2606:4700:4700::1111', 'fe80::1'],
])
def test_mixed_dns_rejected(monkeypatch, addresses):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: [answer(ip) for ip in addresses])
    with pytest.raises(NetworkAccessDenied):
        NetworkPolicy().resolve('mixed.example', 443)


@pytest.mark.parametrize('host', ['2130706433', '0177.0.0.1', '0x7f000001', '127.1'])
def test_numeric_aliases_checked_after_resolution(monkeypatch, host):
    lookup = Mock(return_value=[answer('127.0.0.1')])
    monkeypatch.setattr(socket, 'getaddrinfo', lookup)
    with pytest.raises(NetworkAccessDenied):
        NetworkPolicy().resolve(host, 443)
    lookup.assert_called_once()


@pytest.mark.parametrize('host', [
    'metadata.tencentyun.com', 'METADATA.GOOGLE.INTERNAL.', 'instance-data',
    'instance-data.ec2.internal', 'child.metadata.google.internal',
    'metadata.goog', 'fe80::1%eth0', 'user@example.com', 'example.com\n',
])
def test_invalid_and_metadata_names_never_resolve(monkeypatch, host):
    lookup = Mock(side_effect=AssertionError('DNS must not run'))
    monkeypatch.setattr(socket, 'getaddrinfo', lookup)
    with pytest.raises(NetworkAccessDenied):
        NetworkPolicy().resolve(host, 443)
    lookup.assert_not_called()


@pytest.mark.parametrize('origin', [
    'file:///etc/passwd', 'data:text/plain,test', 'http://user:pass@localhost',
    'http://localhost:0', 'http://localhost:65536', 'http://localhost:bad',
    'http://localhost:', 'http://[::1%25lo]:80', 'http://localhost/path',
    'http://localhost/?token=secret', 'http://localhost\r\n',
    'http://localhost\\@example.com', 'https://[localhost]:443',
])
def test_invalid_origins(origin):
    with pytest.raises(NetworkAccessDenied):
        NetworkPolicy([origin])


def test_explicit_origin_normalization_and_scope(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, *args: [answer('127.0.0.1', port)])
    policy = NetworkPolicy(['HTTP://LOCALHOST.:8123/'])
    assert policy.resolve('localhost', 8123, 'http')[0][4] == ('127.0.0.1', 8123)
    for host, port, scheme in [('localhost', 8124, 'http'), ('localhost', 8123, 'https'),
                               ('other.example', 8123, 'http')]:
        with pytest.raises(NetworkAccessDenied):
            policy.resolve(host, port, scheme)


@pytest.mark.parametrize('address', ['10.1.2.3', '172.16.1.1', '192.168.1.1', '127.0.0.1', '::1', 'fd00::1'])
def test_explicit_private_origin(monkeypatch, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: [answer(address)])
    assert NetworkPolicy(['https://trusted.example']).resolve('trusted.example', 443)


@pytest.mark.parametrize('address', [
    '169.254.169.254', '100.100.100.200', '0.0.0.0', '224.0.0.1',
    '240.0.0.1', 'fe80::1', '::ffff:127.0.0.1', '168.63.129.16',
    'fd00:ec2::254', 'fd20:ce::254',
])
def test_origin_cannot_exempt_forbidden_classes(monkeypatch, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: [answer(address)])
    with pytest.raises(NetworkAccessDenied):
        NetworkPolicy(['https://trusted.example']).resolve('trusted.example', 443)


def test_rebinding_connect_uses_validated_numeric_address(monkeypatch):
    lookup = Mock(side_effect=[[answer('8.8.8.8')], [answer('127.0.0.1')]])
    connection = Mock()
    factory = Mock(return_value=connection)
    monkeypatch.setattr(socket, 'getaddrinfo', lookup)
    monkeypatch.setattr(socket, 'socket', factory)
    proxy = BrowserNetworkProxy()
    assert proxy._connect('changing.example', 443, 'https') is connection
    connection.connect.assert_called_once_with(('8.8.8.8', 443))
    with pytest.raises(NetworkAccessDenied):
        proxy._connect('changing.example', 443, 'https')
    assert factory.call_count == 1
    proxy.close()
    connection.close.assert_called_once()


def test_public_candidates_keep_getaddrinfo_shape(monkeypatch):
    candidates = [answer('8.8.8.8'), answer('2606:4700:4700::1111')]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: candidates)
    assert NetworkPolicy().resolve('public.example', 443) == candidates


def test_loopback_client_only():
    from tools.browser_network import _Server

    assert _Server.verify_request(None, None, ('127.0.0.1', 12345))
    assert not _Server.verify_request(None, None, ('192.168.1.2', 12345))
    assert not _Server.verify_request(None, None, ('8.8.8.8', 12345))


def test_mixed_results_never_open_socket(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: [answer('8.8.8.8'), answer('10.0.0.1')])
    factory = Mock(side_effect=AssertionError('Must reject before socket creation'))
    monkeypatch.setattr(socket, 'socket', factory)
    with pytest.raises(NetworkAccessDenied):
        BrowserNetworkProxy()._connect('mixed.example', 443, 'https')
    factory.assert_not_called()


@pytest.fixture
def origin_server():
    records = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_GET(self):
            records.append((self.path, dict(self.headers), b''))
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', 'http://127.0.0.1:1/private?token=secret')
                body = b''
            else:
                self.send_response(200)
                body = b'local response'
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.headers.get('Transfer-Encoding') == 'chunked':
                pieces = []
                while True:
                    size = int(self.rfile.readline().strip(), 16)
                    if not size:
                        self.rfile.readline()
                        break
                    pieces.append(self.rfile.read(size))
                    assert self.rfile.read(2) == b'\r\n'
                body = b''.join(pieces)
            else:
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            records.append((self.path, dict(self.headers), body))
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:%d' % server.server_port, records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextlib.contextmanager
def running_proxy(origins=()):
    proxy = BrowserNetworkProxy(origins)
    endpoint = urlsplit(proxy.start())
    try:
        yield proxy, endpoint
    finally:
        proxy.close()


def request(endpoint, target, method='GET', body=None, headers=None, chunked=False):
    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
    try:
        connection.request(method, target, body=body, headers=headers or {}, encode_chunked=chunked)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_real_http_denied_and_explicitly_allowed(origin_server):
    origin, records = origin_server
    with running_proxy() as (_, endpoint):
        status, _, body = request(endpoint, origin + '/?token=secret')
        assert status == 403
        assert b'secret' not in body
        assert records == []
    with running_proxy([origin]) as (_, endpoint):
        status, _, body = request(endpoint, origin + '/hello?x=1', headers={
            'Proxy-Authorization': 'secret', 'Proxy-Connection': 'keep-alive',
            'Connection': 'keep-alive, X-Remove', 'X-Remove': 'hidden',
        })
        assert status == 200
        assert body == b'local response'
    path, headers, _ = records[0]
    assert path == '/hello?x=1'
    assert headers['Host'] == urlsplit(origin).netloc
    assert headers['Connection'] == 'close'
    assert 'Proxy-Authorization' not in headers
    assert 'Proxy-Connection' not in headers
    assert 'X-Remove' not in headers


@pytest.mark.parametrize('chunked', [False, True])
def test_post_streaming_body(origin_server, chunked):
    origin, records = origin_server
    body = b'body\x00data' * 20000
    payload = iter([body[:70000], body[70000:]]) if chunked else body
    with running_proxy([origin]) as (_, endpoint):
        status, _, result = request(endpoint, origin + '/post', 'POST', payload, chunked=chunked)
    assert status == 200
    assert result == body
    assert records[-1][2] == body


def test_redirect_target_revalidated(origin_server):
    origin, records = origin_server
    with running_proxy([origin]) as (_, endpoint):
        status, headers, _ = request(endpoint, origin + '/redirect')
        assert status == 302
        status, _, body = request(endpoint, headers['Location'])
        assert status == 403
        assert b'secret' not in body
    assert len(records) == 1


@pytest.mark.parametrize('target', [
    'file:///etc/passwd', 'data:text/plain,secret', 'http://user:secret@127.0.0.1/',
    'http://127.0.0.1:99999/?token=secret', 'http://[::1%25lo]/',
])
def test_http_invalid_targets_do_not_leak(target):
    with running_proxy() as (_, endpoint):
        status, _, body = request(endpoint, target, headers={'Host': '127.0.0.1'})
        assert status == 403
        assert b'secret' not in body


@pytest.mark.parametrize('authority', [
    '127.0.0.1', 'user:secret@127.0.0.1:443', '127.0.0.1:443/path',
    '127.0.0.1:0', '127.0.0.1:65536', '[::1%lo]:443', 'http://example.com:443',
])
def test_connect_invalid_authority(authority):
    with running_proxy() as (_, endpoint):
        status, _, body = request(endpoint, authority, 'CONNECT')
        assert status == 403
        assert b'secret' not in body


def test_connect_denied_and_allowed_private_tunnel(origin_server):
    origin, records = origin_server
    authority = urlsplit(origin).netloc
    with running_proxy([origin]) as (_, endpoint):
        status, _, _ = request(endpoint, authority, 'CONNECT')
        assert status == 403
    with running_proxy(['https://' + authority]) as (_, endpoint):
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        try:
            connection.set_tunnel('127.0.0.1', urlsplit(origin).port)
            connection.request('GET', '/through-tunnel')
            response = connection.getresponse()
            assert response.status == 200
            assert response.read() == b'local response'
        finally:
            connection.close()
    assert records[-1][0] == '/through-tunnel'


def test_connect_preserves_arbitrary_tls_bytes():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(3)
    port = listener.getsockname()[1]
    seen = []

    def echo():
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(3)
            data = connection.recv(4096)
            seen.append(data)
            connection.sendall(data)

    thread = threading.Thread(target=echo, daemon=True)
    thread.start()
    try:
        with running_proxy(['https://127.0.0.1:%d' % port]) as (_, endpoint):
            connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
            try:
                connection.set_tunnel('127.0.0.1', port)
                connection.connect()
                payload = b'\x16\x03\x01\x00\x08TLS-SNI!'
                connection.sock.sendall(payload)
                assert connection.sock.recv(4096) == payload
            finally:
                connection.close()
        assert seen == [payload]
    finally:
        listener.close()
        thread.join(timeout=3)


def test_connection_failure_is_generic_502(monkeypatch):
    with running_proxy() as (proxy, endpoint):
        monkeypatch.setattr(proxy, '_connect', Mock(side_effect=OSError('private trace secret')))
        status, _, body = request(endpoint, 'http://public.example/?token=secret')
        assert status == 502
        assert b'secret' not in body
        assert b'trace' not in body


def test_rejects_host_mismatch_and_ambiguous_body(origin_server):
    origin, records = origin_server
    with running_proxy([origin]) as (_, endpoint):
        status, _, _ = request(endpoint, origin + '/', headers={'Host': 'other.example'})
        assert status == 403
        status, _, _ = request(endpoint, origin + '/', 'POST', b'test', {
            'Content-Length': '4', 'Transfer-Encoding': 'chunked',
        })
        assert status == 403
    assert not records


def test_close_releases_listener_thread_and_live_sockets(origin_server):
    origin, _ = origin_server
    proxy = BrowserNetworkProxy(['https://' + urlsplit(origin).netloc])
    endpoint = proxy.start()
    assert proxy.start() == endpoint
    parsed = urlsplit(endpoint)
    thread = proxy._thread
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    try:
        connection.set_tunnel('127.0.0.1', urlsplit(origin).port)
        connection.connect()
        proxy.close()
        proxy.close()
        assert not thread.is_alive()
        assert not proxy._connections
        assert connection.sock.recv(1) == b''
        with pytest.raises(OSError):
            socket.create_connection((parsed.hostname, parsed.port), timeout=1)
    finally:
        connection.close()
        proxy.close()


def test_idle_tunnel_is_bounded(monkeypatch, origin_server):
    origin, _ = origin_server
    monkeypatch.setattr('tools.browser_network._IDLE_TIMEOUT', 0.15)
    with running_proxy(['https://' + urlsplit(origin).netloc]) as (_, endpoint):
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        try:
            connection.set_tunnel('127.0.0.1', urlsplit(origin).port)
            connection.connect()
            assert connection.sock.recv(1) == b''
        finally:
            connection.close()
