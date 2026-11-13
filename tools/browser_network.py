import ipaddress
import re
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class NetworkAccessDenied(ValueError):
    pass


_PRIVATE = tuple(ipaddress.ip_network(value) for value in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8',
    'fc00::/7', '::1/128',
))
_TRANSITION = tuple(ipaddress.ip_network(value) for value in (
    '64:ff9b::/96', '64:ff9b:1::/48', '2002::/16', '2001::/32',
))
_METADATA = ('metadata.tencentyun.com', 'metadata.google.internal',
             'instance-data', 'instance-data.ec2.internal', 'metadata.goog')
_CHUNK = 65536
_IDLE_TIMEOUT = 15.0


def _deny():
    raise NetworkAccessDenied('Network destination denied')


def _clean(value):
    if not isinstance(value, str) or not value or any(
        ord(char) <= 32 or ord(char) == 127 for char in value
    ):
        _deny()
    return value


def _host(value):
    _clean(value)
    if any(char in value for char in '%/@?#\\'):
        _deny()
    value = value.rstrip('.').lower()
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if ':' in value:
            _deny()
        try:
            value = value.encode('idna').decode('ascii')
        except UnicodeError:
            _deny()
        if len(value) > 253 or not all(
            re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
            for label in value.split('.')
        ):
            _deny()
    else:
        value = str(address)
    if any(value == name or value.endswith('.' + name) for name in _METADATA):
        _deny()
    return value


def _port(value):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)):
        _deny()
    try:
        result = int(value)
    except (ValueError, OverflowError):
        _deny()
    if not 1 <= result <= 65535:
        _deny()
    return result


def _authority(value, default=None):
    _clean(value)
    if any(char in value for char in '/?#@\\%'):
        _deny()
    if value.startswith('['):
        end = value.find(']')
        if end < 0 or ':' not in value[1:end]:
            _deny()
        host = value[1:end]
        suffix = value[end + 1:]
        if suffix and not suffix.startswith(':'):
            _deny()
        port = _port(suffix[1:]) if suffix else default
    else:
        if value.count(':') > 1:
            _deny()
        host, separator, text = value.partition(':')
        port = _port(text) if separator else default
    if port is None:
        _deny()
    return _host(host), port


def _url(value):
    _clean(value)
    if '\\' in value:
        _deny()
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in ('http', 'https') or parsed.fragment:
            _deny()
        host, port = _authority(parsed.netloc, 443 if scheme == 'https' else 80)
    except ValueError:
        _deny()
    return scheme, host, port, parsed


class NetworkPolicy:
    def __init__(self, allowed_origins=()):
        self.allowed_origins = set()
        for origin in allowed_origins:
            scheme, host, port, parsed = _url(origin)
            if parsed.path not in ('', '/') or parsed.query:
                _deny()
            self.allowed_origins.add((scheme, host, port))

    def resolve(self, host, port, scheme='https'):
        host, port = _host(host), _port(port)
        if scheme not in ('http', 'https'):
            _deny()
        trusted = (scheme, host, port) in self.allowed_origins
        candidates = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if not candidates:
            _deny()
        validated = []
        for family, socktype, proto, canonname, sockaddr in candidates:
            if family not in (socket.AF_INET, socket.AF_INET6) or socktype != socket.SOCK_STREAM:
                _deny()
            try:
                address = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                _deny()
            if address.version != (4 if family == socket.AF_INET else 6):
                _deny()
            if sockaddr[1] != port or (family == socket.AF_INET6 and (
                len(sockaddr) != 4 or sockaddr[2] != 0 or sockaddr[3] != 0
            )):
                _deny()
            if (address.is_unspecified or address.is_multicast or address.is_link_local
                    or (address.is_reserved and not address.is_loopback)
                    or str(address) in ('168.63.129.16', 'fd00:ec2::254', 'fd20:ce::254')
                    or getattr(address, 'ipv4_mapped', None)
                    or any(address in network for network in _TRANSITION)):
                _deny()
            if not address.is_global and not (
                trusted and any(address in network for network in _PRIVATE)
            ):
                _deny()
            numeric = (str(address), port) if family == socket.AF_INET else (str(address), port, 0, 0)
            validated.append((family, socket.SOCK_STREAM, proto, canonname, numeric))
        return validated


def _shutdown(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def verify_request(self, request, client_address):
        return ipaddress.ip_address(client_address[0]).is_loopback

    def handle_error(self, request, client_address):
        pass


class BrowserNetworkProxy:
    def __init__(self, allowed_origins=()):
        self.policy = NetworkPolicy(allowed_origins)
        self._lock = threading.RLock()
        self._server = None
        self._thread = None
        self._connections = set()
        self._closing = False

    def start(self):
        with self._lock:
            if self._server is None:
                self._closing = False
                server = _Server(('127.0.0.1', 0), _Handler)
                server.owner = self
                self._server = server
                self._thread = threading.Thread(target=server.serve_forever, daemon=True)
                self._thread.start()
            return 'http://127.0.0.1:%d' % self._server.server_port

    def close(self):
        with self._lock:
            server, thread = self._server, self._thread
            self._closing = True
            connections = tuple(self._connections)
            self._connections.clear()
        for connection in connections:
            _shutdown(connection)
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        with self._lock:
            if self._server is server:
                self._server = None
                self._thread = None

    def _track(self, connection):
        with self._lock:
            if self._closing:
                _shutdown(connection)
                raise OSError('Proxy closed')
            self._connections.add(connection)

    def _release(self, connection):
        with self._lock:
            self._connections.discard(connection)
        _shutdown(connection)

    def _connect(self, host, port, scheme):
        candidates = self.policy.resolve(host, port, scheme)
        deadline = time.monotonic() + _IDLE_TIMEOUT
        for family, socktype, proto, _, sockaddr in candidates:
            connection = socket.socket(family, socktype, proto)
            try:
                self._track(connection)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Connection timeout')
                connection.settimeout(remaining)
                connection.connect(sockaddr)
                connection.settimeout(_IDLE_TIMEOUT)
                return connection
            except OSError:
                self._release(connection)
        raise OSError('Connection failed')


def _relay(source, destination, bidirectional):
    source.setblocking(False)
    destination.setblocking(False)
    peers = {source: destination}
    if bidirectional:
        peers[destination] = source
    buffers = {destination: bytearray()}
    if bidirectional:
        buffers[source] = bytearray()
    reading = set(peers)
    half_closed = set()
    deadline = time.monotonic() + _IDLE_TIMEOUT
    while reading or any(buffers.values()):
        for reader, writer in peers.items():
            if reader not in reading and not buffers[writer] and writer not in half_closed:
                try:
                    writer.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                half_closed.add(writer)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        readable, writable, _ = select.select(
            [item for item in reading if len(buffers[peers[item]]) < _CHUNK],
            [item for item, data in buffers.items() if data], [], remaining,
        )
        for connection in readable:
            try:
                data = connection.recv(_CHUNK - len(buffers[peers[connection]]))
            except BlockingIOError:
                continue
            if data:
                buffers[peers[connection]].extend(data)
                deadline = time.monotonic() + _IDLE_TIMEOUT
            else:
                reading.discard(connection)
        for connection in writable:
            try:
                count = connection.send(buffers[connection])
            except BlockingIOError:
                continue
            if not count:
                return
            del buffers[connection][:count]
            deadline = time.monotonic() + _IDLE_TIMEOUT


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    rbufsize = 0

    def setup(self):
        self.request.settimeout(_IDLE_TIMEOUT)
        self.server.owner._track(self.request)
        super().setup()

    def finish(self):
        try:
            super().finish()
        finally:
            self.server.owner._release(self.request)

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        body = b'Network destination denied\n' if code == 403 else b'Proxy request failed\n'
        try:
            self.send_response_only(code)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def handle_expect_100(self):
        return True

    def __getattr__(self, name):
        if name.startswith('do_'):
            return self._forward
        raise AttributeError(name)

    def _framing(self):
        lengths = self.headers.get_all('Content-Length', [])
        encodings = self.headers.get_all('Transfer-Encoding', [])
        if len(lengths) > 1 or len(encodings) > 1 or (lengths and encodings):
            _deny()
        if encodings:
            if encodings[0].lower().strip() != 'chunked':
                _deny()
            return None
        if lengths:
            if not re.fullmatch(r'[0-9]{1,18}', lengths[0]):
                _deny()
            return int(lengths[0])
        return 0

    def _copy(self, upstream, count):
        while count:
            data = self.rfile.read(min(_CHUNK, count))
            if not data:
                raise OSError('Incomplete body')
            upstream.sendall(data)
            count -= len(data)

    def _body(self, upstream, length):
        if length is not None:
            self._copy(upstream, length)
            return
        while True:
            line = self.rfile.readline(8193)
            if len(line) > 8192 or not line.endswith(b'\r\n'):
                _deny()
            size = line[:-2].split(b';', 1)[0]
            if not re.fullmatch(b'[0-9a-fA-F]{1,16}', size):
                _deny()
            count = int(size, 16)
            if not count:
                total = 0
                while True:
                    trailer = self.rfile.readline(8193)
                    total += len(trailer)
                    if total > 65536 or not trailer.endswith(b'\r\n'):
                        _deny()
                    if trailer == b'\r\n':
                        break
                upstream.sendall(b'0\r\n\r\n')
                return
            upstream.sendall(size + b'\r\n')
            self._copy(upstream, count)
            ending = bytearray()
            while len(ending) < 2:
                part = self.rfile.read(2 - len(ending))
                if not part:
                    raise OSError('Incomplete body')
                ending.extend(part)
            if ending != b'\r\n':
                _deny()
            upstream.sendall(b'\r\n')

    def _forward(self):
        upstream = None
        responded = False
        self.close_connection = True
        try:
            length = self._framing()
            if self.command == 'CONNECT':
                host, port = _authority(self.path)
                if length != 0:
                    _deny()
                upstream = self.server.owner._connect(host, port, 'https')
                self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                responded = True
                _relay(self.connection, upstream, True)
                return
            scheme, host, port, parsed = _url(self.path)
            if scheme != 'http':
                _deny()
            hosts = self.headers.get_all('Host', [])
            if len(hosts) != 1 or _authority(hosts[0], 80) != (host, port):
                _deny()
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", self.command):
                _deny()
            excluded = {'proxy-authorization', 'proxy-connection', 'connection',
                        'keep-alive', 'te', 'trailer', 'upgrade', 'expect',
                        'content-length', 'transfer-encoding'}
            for value in self.headers.get_all('Connection', []):
                excluded.update(item.strip().lower() for item in value.split(','))
            target = parsed.path or '/'
            if parsed.query:
                target += '?' + parsed.query
            headers = []
            for name, value in self.headers.items():
                if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                    _deny()
                if any(ord(char) < 32 and char != '\t' or ord(char) == 127 for char in value):
                    _deny()
                if name.lower() not in excluded or name.lower() == 'host':
                    headers.append('%s: %s\r\n' % (name, value))
            headers.append('Connection: close\r\n')
            if length is None:
                headers.append('Transfer-Encoding: chunked\r\n')
            elif self.headers.get('Content-Length') is not None:
                headers.append('Content-Length: %d\r\n' % length)
            packet = ('%s %s HTTP/1.1\r\n%s\r\n' % (
                self.command, target, ''.join(headers),
            )).encode('latin-1')
            upstream = self.server.owner._connect(host, port, scheme)
            if self.headers.get('Expect', '').lower() == '100-continue':
                self.wfile.write(b'HTTP/1.1 100 Continue\r\n\r\n')
            upstream.sendall(packet)
            self._body(upstream, length)
            first = upstream.recv(_CHUNK)
            if not first:
                raise OSError('Empty upstream response')
            self.wfile.write(first)
            responded = True
            _relay(upstream, self.connection, False)
        except NetworkAccessDenied:
            if not responded:
                self.send_error(403)
        except (OSError, ValueError, UnicodeError):
            if not responded:
                self.send_error(502)
        finally:
            if upstream is not None:
                self.server.owner._release(upstream)
