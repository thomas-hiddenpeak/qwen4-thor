"""Explicit TLS destination and private API key; no redirects/proxy/retries."""
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import time
from urllib.parse import urlsplit

from tools.project_tasks import engine, store


def profile(path):
    path = store.local(path)
    store.private(path)
    try:
        return read_profile(path)
    except store.ContractError as error:
        if error.code == "unsafe_path":
            raise
        raise store.ContractError("invalid client profile", "invalid_profile") from None
    except (ValueError, KeyError, TypeError, AttributeError):
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
    ca = Path(value["ca_file"])
    store.require(ca.is_absolute() and not ca.is_symlink() and ca.is_file() and
                  ca.stat().st_size <= 65536, "explicit CA file required")
    value["base_url"] = f"https://{url.netloc}/v1"
    public = {key: value[key] for key in ("client_id", "base_url", "connect_host",
              "model", "protocol_sha256", "template_sha256")}
    public["ca_sha256"] = hashlib.sha256(ca.read_bytes()).hexdigest()
    public["key_sha256"] = hashlib.sha256(value["api_key"].encode()).hexdigest()
    return value, public


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
        self.value = value
        self.context = ssl.create_default_context(cafile=value["ca_file"])

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
