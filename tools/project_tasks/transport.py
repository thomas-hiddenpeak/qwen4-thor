"""Explicit TLS destination and private API key; no redirects/proxy/retries."""
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import time
from types import MappingProxyType
from urllib.parse import urlsplit

from tools.project_tasks import engine, store


@dataclass(frozen=True)
class PreparedProfile(Mapping):
    data: Mapping
    ca_pem: bytes

    def __getitem__(self, key): return self.data[key]
    def __iter__(self): return iter(self.data)
    def __len__(self): return len(self.data)


def ca_context(raw):
    # No ambient trust store or SSLKEYLOGFILE. Both the binding and each
    # connection use these exact bytes, never a second read of ca_file.
    blocks = re.findall(rb"-----BEGIN CERTIFICATE-----[\r\n]+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----", raw)
    store.require(1 <= len(blocks) <= 8 and not re.sub(
        rb"-----BEGIN CERTIFICATE-----[\r\n]+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----", b"", raw).strip(),
        "explicit bounded PEM CA bundle required", code='invalid_profile')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=raw.decode('ascii'))
    store.require(context.cert_store_stats()['x509_ca'] > 0,
                  "CA certificate required", code='invalid_profile')
    return context


def ca_snapshot(path):
    path = Path(path)
    store.require(path.is_absolute() and not path.is_symlink(),
                  "explicit CA file required", code='invalid_profile')
    before = path.lstat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        signature = lambda row: (row.st_dev, row.st_ino, row.st_size,
                                row.st_mtime_ns, row.st_ctime_ns)
        store.require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 65536 and
            signature(info) == signature(before), "bounded CA file changed", code='invalid_profile')
        raw = stream.read(65537)
        store.require(len(raw) == info.st_size and
            signature(os.fstat(stream.fileno())) == signature(info) == signature(path.lstat()),
            "CA changed during read", code='invalid_profile')
    ca_context(raw)
    return raw


def profile(path):
    path = store.local(path)
    store.private(path)
    try:
        return read_profile(path)
    except store.ContractError as error:
        if error.code == "unsafe_path":
            raise
        raise store.ContractError("invalid client profile", "invalid_profile") from None
    except (ValueError, KeyError, TypeError, AttributeError, ssl.SSLError):
        raise store.ContractError("invalid client profile", "invalid_profile") from None


def read_profile(path):
    store.require(path.stat().st_size <= 16384, "profile size")
    value = engine.chat.load_json(path.read_bytes())
    store.require(set(value) == {"client_id", "base_url", "connect_host", "ca_file",
        "api_key", "model", "protocol_sha256", "template_sha256"}, "profile fields")
    store.task_name(value["client_id"], "profile-check")
    url = urlsplit(value["base_url"])
    store.require(url.scheme == "https" and url.hostname and not url.username and
        not url.password and not url.query and not url.fragment and
        url.path.rstrip("/") in ("", "/v1"), "explicit TLS origin required")
    store.require(url.port is None or 1 <= url.port <= 65535, "TLS port")
    address = ipaddress.ip_address(value["connect_host"])
    store.require(address.is_private and not address.is_unspecified and
                  not address.is_multicast, "controlled LAN/loopback destination required")
    store.require(isinstance(value["api_key"], str) and
        32 <= len(value["api_key"]) <= 256 and
        all(33 <= ord(c) <= 126 for c in value["api_key"]), "API key shape")
    for key in ("protocol_sha256", "template_sha256"):
        text = value[key]
        store.require(isinstance(text, str) and len(text) == 64 and
                      all(c in "0123456789abcdef" for c in text), "protocol/template identity")
    store.require(isinstance(value["model"], str) and 0 < len(value["model"]) <= 128,
                  "model identity")
    ca = ca_snapshot(value["ca_file"])
    value["base_url"] = f"https://{url.netloc}/v1"
    public = {key: value[key] for key in ("client_id", "base_url", "connect_host",
              "model", "protocol_sha256", "template_sha256")}
    public["ca_sha256"] = hashlib.sha256(ca).hexdigest()
    public["key_sha256"] = hashlib.sha256(value["api_key"].encode()).hexdigest()
    return PreparedProfile(MappingProxyType(value), ca), public


class Connection(http.client.HTTPSConnection):
    def __init__(self, value, context, timeout):
        url = urlsplit(value["base_url"])
        super().__init__(url.hostname, url.port or 443, context=context, timeout=timeout)
        self.connect_host = value["connect_host"]

    def connect(self):
        sock = socket.create_connection((self.connect_host, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class DeadlineReader:
    def __init__(self, response, sock, deadline):
        self.response, self.sock, self.deadline = response, sock, deadline
        self.buffer = bytearray()
        self.eof = False

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request wall deadline")
        self.sock.settimeout(remaining)

    def read(self, size=-1):
        store.require(0 <= size <= store.MAX_WIRE_BYTES + 1, "bounded reads required")
        while len(self.buffer) < size and not self.eof:
            self.fill(size - len(self.buffer))
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def readline(self, size=-1):
        store.require(0 < size <= store.MAX_WIRE_BYTES + 1, "bounded lines required")
        while True:
            newline = self.buffer.find(b"\n", 0, size)
            if newline >= 0:
                size = newline + 1
                break
            if len(self.buffer) >= size or self.eof:
                break
            self.fill(size - len(self.buffer))
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def fill(self, size):
        # Content-Length completion can retire the last socket IO reference in
        # read1(). EOF then comes from HTTPResponse, not the retired descriptor.
        if self.response.isclosed():
            self.eof = True
            return
        self.remaining()
        data = self.response.read1(min(size, 65536))
        self.eof = not data
        self.buffer.extend(data)


class Client:
    def __init__(self, value):
        store.require(isinstance(value, PreparedProfile),
                      "validated profile snapshot required", code='invalid_profile')
        self.value = dict(value)
        self.context = ca_context(value.ca_pem)

    def request(self, method, route, body, timeout, consume, *, trace_id=None, error_sink=None):
        deadline = time.monotonic() + timeout
        conn = Connection(self.value, self.context, timeout)
        headers = {"Authorization": "Bearer " + self.value["api_key"],
                   "Accept": "application/json", "Connection": "close"}
        raw = None
        if body is not None:
            raw = store.encode(body)
            store.require(len(raw) <= store.MAX_EVENT_BYTES, "request byte budget")
            headers["Content-Type"] = "application/json"
            identifier = trace_id or body.get("request_id")
            if identifier is not None:
                headers["X-Request-Id"] = identifier
        try:
            conn.request(method, "/v1" + route, raw, headers)
            sock = conn.sock
            response = conn.getresponse()
            with response:
                reader = DeadlineReader(response, sock, deadline)
                if response.status != 200:
                    # Keep errors bounded; redirect responses are never followed.
                    raw_error = reader.read(65537)
                    if error_sink:
                        error_sink(raw_error)
                    raise engine.chat.RemoteError(response.status, raw_error)
                return consume(response, reader)
        finally:
            conn.close()

    def capabilities(self, **expected):
        def consume(response, reader):
            store.require(response.headers.get_content_type() == "application/json", "capability content type")
            raw = reader.read(65537)
            store.require(len(raw) <= 65536, "capability size")
            return engine.chat.validate_capabilities(engine.chat.load_json(raw), **expected)
        return self.request("GET", "/capabilities", None, 10, consume)

    def complete(self, body, *, sink):
        def consume(response, reader):
            expected = "text/event-stream" if body["stream"] else "application/json"
            store.require(response.headers.get_content_type() == expected, "response content type")
            parser = engine.chat.read_sse if body["stream"] else engine.chat.read_json
            return parser(reader, sink=sink, max_bytes=store.MAX_WIRE_BYTES)
        return self.request("POST", "/chat/completions", body,
                            body["request_timeout_ms"] / 1000 + 10, consume)

    def preflight(self, body, *, sink):
        from tools.project_tasks import budget
        def consume(response, reader):
            store.require(response.headers.get_content_type() == "application/json", "preflight content type")
            raw = reader.read(budget.MAX_CHECK_BYTES + 1)
            store.require(len(raw) <= budget.MAX_CHECK_BYTES, "preflight response size")
            sink(raw)
            return engine.chat.load_json(raw)
        envelope = dict(request=body, include_prompt=False, include_token_ids=False)
        return self.request("POST", "/chat/preflight", envelope, 30, consume,
                            trace_id=body["request_id"], error_sink=sink)
