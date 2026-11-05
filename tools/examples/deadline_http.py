"""Standard-library HTTP with one budget across resolution, TLS and all IO.

No HTTP is sent on import. Callbacks/CPU work are not preempted; readers check
the deadline again before returning or committing a successful response.
"""
import http.client
import io
import ipaddress
import json
import socket
import subprocess
import sys
import time
import urllib.request


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("request network deadline")
    return seconds


_RESOLVE = """import json,socket,sys
rows=socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)
print(json.dumps([(r[0],r[4]) for r in rows[:16]]))
"""


def resolve(host, port, deadline):
    remaining(deadline)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        return [(socket.AF_INET, (host, port))] if address.version == 4 else [
            (socket.AF_INET6, (host, port, 0, 0))]
    # A Python thread cannot cancel libc getaddrinfo. Own a short-lived child,
    # including its timeout cleanup; do not leave an unbounded resolver thread.
    child = subprocess.Popen([sys.executable, "-I", "-S", "-c", _RESOLVE,
                              host, str(port)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
    try:
        try:
            raw, _ = child.communicate(timeout=remaining(deadline))
        except subprocess.TimeoutExpired:
            raise TimeoutError("DNS resolution deadline") from None
        remaining(deadline)
        if child.returncode or len(raw) > 65536:
            raise OSError("DNS resolution failed")
        rows = json.loads(raw)
        if not rows or len(rows) > 16:
            raise OSError("DNS address limit")
        return [(family, tuple(address)) for family, address in rows]
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=1)


class DeadlineInput(io.RawIOBase):
    def __init__(self, raw, sock, deadline):
        self.raw, self.sock, self.deadline = raw, sock, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(remaining(self.deadline))
        count = self.raw.readinto(buffer)
        remaining(self.deadline)
        return count

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class DeadlineSocket:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError("HTTP response reader required")
        raw = self.sock.makefile(mode, buffering=0)
        try:
            return io.BufferedReader(DeadlineInput(raw, self.sock, self.deadline))
        except BaseException:
            raw.close()
            raise

    def sendall(self, data):
        with memoryview(data).cast("B") as view:
            offset = 0
            while offset < len(view):
                self.sock.settimeout(remaining(self.deadline))
                count = self.sock.send(view[offset:offset + 65536])
                remaining(self.deadline)
                if count <= 0:
                    raise ConnectionError("request write failed")
                offset += count


class FramedResponse(http.client.HTTPResponse):
    """Reject EOF that leaves an advertised HTTP body incomplete."""

    def read(self, amt=None):
        length = self.length
        value = super().read(amt)
        if length is not None:
            expected = length if amt is None or amt < 0 else min(length, amt)
            if len(value) < expected:
                raise http.client.IncompleteRead(value, expected - len(value))
        return value

    def readline(self, limit=-1):
        value = super().readline(limit)
        if limit != 0 and not value and self.length not in (None, 0):
            raise http.client.IncompleteRead(value, self.length)
        return value

    def _read_and_discard_trailer(self):
        # http.client otherwise accepts EOF in place of the terminal empty
        # trailer line. A valid JSON/SSE payload cannot repair broken framing.
        for _ in range(101):
            line = self.fp.readline(65537)
            if len(line) > 65536:
                raise http.client.LineTooLong("trailer line")
            if not line:
                raise http.client.IncompleteRead(b"")
            if line in (b"\r\n", b"\n"):
                return
        raise http.client.HTTPException("too many trailer fields")


class Connection(http.client.HTTPConnection):
    response_class = FramedResponse

    def __init__(self, host, *, timeout, deadline, context=None,
                 connect_host=None, **kwargs):
        self.default_port = 443 if context is not None else 80
        super().__init__(host, timeout=timeout, **kwargs)
        self.deadline, self.context = deadline, context
        self.connect_host = connect_host or self.host

    def connect(self):
        if self._tunnel_host:
            raise ValueError("proxy tunnelling is not supported")
        addresses = resolve(self.connect_host, self.port, self.deadline)
        failure = None
        for family, address in addresses:
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.settimeout(remaining(self.deadline))
                sock.connect(address)
            except OSError as error:
                sock.close()
                failure = error
                remaining(self.deadline)
                continue
            try:
                remaining(self.deadline)
                if self.context is not None:
                    sock.settimeout(remaining(self.deadline))
                    sock = self.context.wrap_socket(sock, server_hostname=self.host)
                remaining(self.deadline)
                self.sock = DeadlineSocket(sock, self.deadline)
                return
            except BaseException:
                sock.close()
                raise
        raise failure or OSError("no usable DNS addresses")


class HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, connect_host=None):
        super().__init__()
        self.connect_host = connect_host

    def http_open(self, request):
        return self.do_open(lambda host, **kwargs: Connection(
            host, deadline=request.q4t_deadline, connect_host=self.connect_host,
            **kwargs), request)


class HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context, connect_host=None):
        super().__init__(context=context)
        self.connect_host = connect_host

    def https_open(self, request):
        return self.do_open(lambda host, **kwargs: Connection(
            host, deadline=request.q4t_deadline, context=self._context,
            connect_host=self.connect_host, **kwargs), request)


class Response:
    def __init__(self, response, deadline):
        self.response, self.deadline = response, deadline

    def __getattr__(self, name):
        return getattr(self.response, name)

    def __enter__(self):
        return self

    def __exit__(self, kind, _error, _traceback):
        self.response.close()
        if kind is None:
            remaining(self.deadline)

    def read(self, size=-1):
        remaining(self.deadline)
        value = self.response.read(size)
        remaining(self.deadline)
        return value

    def readline(self, size=-1):
        remaining(self.deadline)
        value = self.response.readline(size)
        remaining(self.deadline)
        return value
