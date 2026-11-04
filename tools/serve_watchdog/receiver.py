"""Bounded loopback HTTPS validation fixture, never a production receiver."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import secrets
import ssl
import threading
import time

from tools.serve_watchdog import core as c, watchdog as w
from tools.serve_notify import notify as n


class Receiver:
    def __init__(self, root, state, digest, token, cert, key, port):
        self.root = Path(root); self.root.mkdir(mode=0o700)
        self.rows, self.errors = [], []
        self.lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_): pass

            def setup(self):
                super().setup(); self.connection.settimeout(3)

            def do_POST(self):
                status, raw, result = 400, b'', None
                auth = False
                try:
                    with receiver.lock:
                        c.require(len(receiver.rows) < 64, 'receiver_budget')
                    c.require(self.path == '/alerts' and
                        len(self.headers.get_all('Authorization', [])) == 1,
                        'route_or_auth_header')
                    auth = secrets.compare_digest(
                        self.headers['Authorization'], 'Bearer '+token)
                    if not auth:
                        status = 401
                    else:
                        c.require(not self.headers.get('Transfer-Encoding') and
                            len(self.headers.get_all('Content-Length', [])) == 1,
                            'fixture_framing')
                        length = int(self.headers['Content-Length'])
                        c.require(0 < length <= c.MAXIMUM, 'fixture_body_size')
                        raw = self.rfile.read(length)
                        c.require(len(raw) == length, 'fixture_incomplete_body')
                        value = c.parse(raw)
                        names = {a['labels']['alertname'] for a in value['alerts']}
                        if 'Q4TNotifyHeartbeat' in names:
                            result = w.ingest(state, digest, raw,
                                              self.headers['Authorization'])
                            status = 204
                        else:
                            # Isolated test-only sink for synthetic/down-target
                            # alerts. This is not a production receiver policy.
                            result = dict(fixture_nonheartbeat=True)
                            status = 202
                except c.ContractError as error:
                    status = 422
                    result = dict(rejected=str(error))
                except BaseException as error:
                    status = 503
                    with receiver.lock:
                        receiver.errors.append(type(error).__name__)
                with receiver.lock:
                    index = len(receiver.rows)
                    row = dict(index=index, status=status,
                        authorization_valid=auth, path=self.path,
                        monotonic=time.monotonic(), unix_seconds=time.time(),
                        payload_sha256=hashlib.sha256(raw).hexdigest(),
                        result=result)
                    n.m.g.save(receiver.root/f'{index:03d}-body.json', raw)
                    n.pkg.save(receiver.root/f'{index:03d}-receipt.json', row)
                    receiver.rows.append(row)
                self.send_response(status)
                self.send_header('Content-Length', '0')
                self.send_header('Connection', 'close')
                self.end_headers(); self.close_connection = True

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = False
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(5)
        c.require(not self.thread.is_alive(), 'receiver_cleanup')

