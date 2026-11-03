"""Private, bounded local HTTPS acceptance receiver, never a deployment sink."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import ssl
import threading
import time

from tools.serve_notify import notify as n


def payload(raw):
    value = json.loads(raw)
    n.require(value['version'] == '4' and value['receiver'] == 'q4t-webhook' and
              value['status'] in ('firing', 'resolved') and
              value['truncatedAlerts'] == 0 and 1 <= len(value['alerts']) <= 32,
              'invalid webhook envelope')
    for alert in value['alerts']:
        n.require(alert['status'] in ('firing', 'resolved') and
                  alert['labels']['deployment'] == 'thor-acceptance' and
                  len(alert['fingerprint']) == 16 and alert['startsAt'] and
                  alert['endsAt'], 'invalid webhook alert identity')
    return value


class Receiver:
    def __init__(self, root, cert, key, token, port=19445):
        self.root, self.token = root, token
        self.rows, self.errors = [], []
        self.lock = threading.RLock()
        self.redirect = True
        self.failed_once = set()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_): pass

            def setup(self):
                super().setup(); self.connection.settimeout(3)

            def do_POST(self):
                status = 400
                try:
                    n.require(not self.headers.get('Transfer-Encoding'), 'unexpected chunked fixture input')
                    length = int(self.headers['Content-Length'])
                    n.require(0 < length <= 128*1024, 'fixture request size bound')
                    raw = self.rfile.read(length)
                    n.require(len(raw) == length, 'incomplete fixture body')
                    value = payload(raw)
                    auth = secrets.compare_digest(self.headers.get('Authorization', ''),
                                                  'Bearer '+receiver.token)
                    names = {a['labels']['alertname'] for a in value['alerts']}
                    with receiver.lock:
                        n.require(len(receiver.rows) < 64, 'receiver attempt budget')
                        status = 204 if auth and self.path == '/alerts' else 401
                        for name in names & {'Q4TNotifyHeartbeat', 'Q4TTargetDown'}:
                            if value['status'] == 'firing' and name not in receiver.failed_once:
                                receiver.failed_once.add(name); status = 503
                        if 'Q4TDeliveryProbe' in names and receiver.redirect:
                            status = 307
                        index = len(receiver.rows)
                        n.m.g.save(receiver.root/f'{index:03d}-body.json', raw)
                        row = dict(index=index, path=self.path, authorization_valid=auth,
                            response_status=status, payload=value,
                            sha256=hashlib.sha256(raw).hexdigest(),
                            monotonic=time.monotonic(), unix_seconds=time.time())
                        receiver.rows.append(row)
                        n.pkg.save(receiver.root/f'{index:03d}-receipt.json', row)
                except BaseException as error:
                    with receiver.lock: receiver.errors.append(repr(error))
                self.send_response(status)
                if status == 307: self.send_header('Location', '/redirected')
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

    def selected(self, name, status, code=204):
        with self.lock:
            n.require(not self.errors, 'receiver failed: '+str(self.errors))
            return [r for r in self.rows if r['response_status'] == code and
                    any(a['labels']['alertname'] == name and a['status'] == status
                        for a in r['payload']['alerts'])]

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(5)
        n.require(not self.thread.is_alive(), 'receiver remained')
