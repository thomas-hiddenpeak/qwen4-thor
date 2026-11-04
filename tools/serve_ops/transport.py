"""Bounded direct loopback HTTP GET: no proxy, redirects, retry or mutation."""
import errno
import re
import select
import socket
import time
from urllib.parse import urlsplit

from .contracts import InvalidObservation

ROUTES = ('/livez', '/readyz', '/v1/capabilities', '/v1/config', '/metrics')
MAX_BODY = 262144
MAX_HEADER = 16384


def endpoint(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or
                parsed.username is not None or parsed.password is not None or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment or
                parsed.port is None or not 1 <= parsed.port <= 65535):
            raise InvalidObservation('direct_loopback_url_required')
        return parsed.port
    except ValueError:
        raise InvalidObservation('direct_loopback_url_required') from None


def parse_response(raw):
    head, separator, body = raw.partition(b'\r\n\r\n')
    if not separator or len(head) > MAX_HEADER or len(body) > MAX_BODY:
        raise InvalidObservation('response_size_or_header')
    lines = head.split(b'\r\n')
    status = re.fullmatch(rb'HTTP/1\.[01] ([1-5][0-9][0-9]) [^\r\n]*', lines[0])
    if not status:
        raise InvalidObservation('invalid_http_status')
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(b':')
        if not sep or not re.fullmatch(rb'[a-zA-Z0-9-]+', name):
            raise InvalidObservation('invalid_http_header')
        name, value = name.lower(), value.strip()
        if name in (b'content-length', b'content-type', b'transfer-encoding'):
            if name in headers:
                raise InvalidObservation('duplicate_http_header')
            headers[name] = value
    if b'transfer-encoding' in headers:
        raise InvalidObservation('unsupported_http_encoding')
    length = headers.get(b'content-length', b'')
    if not re.fullmatch(rb'[0-9]{1,8}', length) or int(length) != len(body):
        raise InvalidObservation('incomplete_http_body')
    media = headers.get(b'content-type', b'').split(b';', 1)[0].strip().lower()
    if media not in (b'application/json', b'text/plain'):
        raise InvalidObservation('unsupported_http_content_type')
    try:
        return dict(http_status=int(status[1]), content_type=media.decode(), body=body.decode('utf-8'))
    except UnicodeError:
        raise InvalidObservation('invalid_http_utf8') from None


def _remaining(until, stopped):
    if stopped is not None and stopped():
        raise InvalidObservation('observation_stopped')
    remaining = until-time.monotonic()
    if remaining <= 0:
        raise InvalidObservation('http_deadline')
    return remaining


def _wait(sock, write, until, stopped):
    while True:
        remaining = _remaining(until, stopped)
        interval = min(.1, remaining) if stopped is not None else remaining
        ready = select.select([] if write else [sock],
                              [sock] if write else [], [], interval)
        # A signal, a late wakeup, or readiness at the deadline cannot turn
        # cancellation/expiry into a successful observation.
        _remaining(until, stopped)
        if ready[1 if write else 0]:
            return


def _connect(port, until, stopped):
    remaining = _remaining(until, stopped)
    if stopped is None:
        return socket.create_connection(('127.0.0.1', port), timeout=remaining)
    # Numeric loopback needs no resolver. Nonblocking connect also bounds
    # stop latency when a listening backend has not begun accepting yet.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        error = sock.connect_ex(('127.0.0.1', port))
        if error in (errno.EINPROGRESS, errno.EWOULDBLOCK,
                     errno.EALREADY, errno.EINTR):
            _wait(sock, True, until, stopped)
            error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if error:
            raise OSError(error, 'loopback connection failed')
        _remaining(until, stopped)
        return sock
    except BaseException:
        sock.close()
        raise


def get(port, path, *, timeout=2, deadline=None, credentials=None,
        stopped=None):
    if path not in ROUTES:
        raise InvalidObservation('unsupported_diagnostic_route')
    headers = ''
    if credentials is not None:
        if (path != '/v1/capabilities' or type(credentials) is not dict or
                credentials.keys() != {'client', 'token'} or
                type(credentials['client']) is not str or
                not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', credentials['client']) or
                type(credentials['token']) is not str or
                not re.fullmatch(r'[a-f0-9]{64}', credentials['token'])):
            raise InvalidObservation('invalid_diagnostic_credentials')
        headers = (f"X-Q4T-Client-Id: {credentials['client']}\r\n"
                   f"X-Q4T-Gateway-Token: {credentials['token']}\r\n")
    until = min(time.monotonic()+timeout, deadline if deadline is not None else float('inf'))
    raw = bytearray()
    try:
        with _connect(port, until, stopped) as sock:
            sock.setblocking(False)
            pending = (f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                       f'{headers}Connection: close\r\n\r\n').encode()
            while pending:
                _wait(sock, True, until, stopped)
                try:
                    sent = sock.send(pending)
                except BlockingIOError:
                    continue
                if not sent:
                    raise InvalidObservation('http_write_failed')
                pending = pending[sent:]
            while True:
                _wait(sock, False, until, stopped)
                try:
                    part = sock.recv(65536)
                except BlockingIOError:
                    continue
                _remaining(until, stopped)
                if not part:
                    result = parse_response(bytes(raw))
                    _remaining(until, stopped)
                    return result
                raw.extend(part)
                boundary = raw.find(b'\r\n\r\n')
                if (len(raw) > MAX_HEADER+MAX_BODY or
                        (boundary < 0 and len(raw) > MAX_HEADER) or boundary > MAX_HEADER):
                    raise InvalidObservation('http_response_limit')
    except (TimeoutError, socket.timeout):
        raise InvalidObservation('http_deadline') from None
    except OSError as error:
        raise InvalidObservation('connection_refused' if error.errno == errno.ECONNREFUSED
                                 else 'http_io_failed') from None
