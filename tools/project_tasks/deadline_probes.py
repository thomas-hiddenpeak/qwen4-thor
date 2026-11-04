"""Real bounded loopback TLS peers; invoked only by the guarded stage."""
import importlib.util
import io
import json
from pathlib import Path
import socket
import ssl
import sys
import threading
import time

from tools.project_tasks import transport, engine, store, test_tasks
from tools.serve_service import package as pkg

CASES = ('handshake', 'status', 'headers', 'chunk-size', 'trailer', 'body',
         'error-body', 'length-ok', 'chunk-ok')
TIMEOUT = .25


class Peer:
    def __init__(self, context, case):
        self.context, self.case = context, case
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(2)
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        self.record = dict(case=case, accepts=0, request_headers=0, sent_bytes=0)
        self.thread = threading.Thread(target=self.serve)

    def send(self, peer, raw, slow=False):
        for part in ([raw] if not slow else [bytes([c]) for c in raw]):
            if self.stop.is_set():
                return
            peer.sendall(part)
            self.record['sent_bytes'] += len(part)
            if slow and self.stop.wait(.03):
                return

    def serve(self):
        try:
            with self.listener.accept()[0] as raw:
                self.record['accepts'] += 1
                raw.settimeout(2)
                if self.case == 'handshake':
                    self.stop.wait(2)
                    return
                with self.context.wrap_socket(raw, server_side=True) as peer:
                    header = bytearray()
                    while b'\r\n\r\n' not in header:
                        part = peer.recv(4096)
                        if not part:
                            return
                        header.extend(part)
                        pkg.require(len(header) < 65536, 'probe request header bound')
                    self.record['request_headers'] += 1
                    # Do not persist request bytes; they include the private Key.
                    start = b'HTTP/1.1 200 OK\r\n'
                    if self.case == 'status':
                        self.send(peer, start, True)
                    else:
                        self.send(peer, start if self.case != 'error-body' else b'HTTP/1.1 503 Busy\r\n')
                    if self.case == 'headers':
                        self.send(peer, b'X-Slow: ' + b'x' * 40 + b'\r\n', True)
                        self.send(peer, b'Content-Length: 2\r\nConnection: close\r\n\r\nok')
                    elif self.case in ('chunk-size', 'trailer', 'chunk-ok'):
                        self.send(peer, b'Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n')
                        if self.case == 'chunk-size':
                            self.send(peer, b'1' * 40 + b'\r\n', True)
                        elif self.case == 'trailer':
                            self.send(peer, b'2\r\nok\r\n0\r\n')
                            self.send(peer, b'X-Slow: ' + b'x' * 40 + b'\r\n\r\n', True)
                        else:
                            self.send(peer, b'2\r\nok\r\n0\r\n\r\n')
                    elif self.case in ('body', 'error-body'):
                        self.send(peer, b'Content-Length: 100\r\nConnection: close\r\n\r\n')
                        self.send(peer, b'x' * 100, True)
                    elif self.case in ('status', 'length-ok'):
                        self.send(peer, b'Content-Length: 2\r\nConnection: close\r\n\r\nok')
        except (OSError, ssl.SSLError) as error:
            self.record['peer_error_type'] = type(error).__name__
        except BaseException as error:
            self.record['unexpected_error'] = type(error).__name__
        finally:
            self.listener.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.listener.close()
        self.thread.join(3)
        pkg.require(not self.thread.is_alive(), 'TLS peer did not terminate')
        pkg.require('unexpected_error' not in self.record, 'TLS peer failed')
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', self.port))
        self.record.update(thread_stopped=True, listener_released=True)


def client_for(module, profile, port):
    # Preserve the already validated CA/Key snapshot. Only this acceptance peer
    # uses a fresh ephemeral port; no production profile is rewritten.
    client = object.__new__(module.Client)
    client.value = dict(profile)
    from urllib.parse import urlsplit
    host = urlsplit(profile['base_url']).hostname
    client.value['base_url'] = f'https://{host}:{port}/v1'
    client.context = module.ca_context(profile.ca_pem)
    return client


def request(client, body=None, sink=None):
    def consume(response, reader):
        raw = reader.read(1024)
        if sink:
            sink(raw)
        return raw
    return client.request('GET' if body is None else 'POST', '/capabilities' if body is None
                          else '/chat/completions', body, TIMEOUT, consume)


def execute(output, profile_path, certificate, key, historical):
    output.mkdir(mode=0o700)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    profile, _ = transport.profile(profile_path)
    spec = importlib.util.spec_from_file_location('q4t_deadline_baseline',
        Path(historical)/'tools/project_tasks/transport.py')
    old = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = old
    spec.loader.exec_module(old)
    rows = []
    for name in ('baseline-headers', *CASES):
        legacy = name == 'baseline-headers'
        case = 'headers' if legacy else name
        with Peer(context, case) as peer:
            client = client_for(old if legacy else transport, profile, peer.port)
            began = time.monotonic()
            error, value = None, None
            try:
                value = request(client)
            except TimeoutError:
                error = 'TimeoutError'
            elapsed = time.monotonic() - began
        row = dict(name=name, elapsed_seconds=elapsed, timeout_seconds=TIMEOUT,
                   error=error, body=value.decode() if value else None, **peer.record)
        pkg.save(output/(name+'.json'), row)
        if legacy:
            pkg.require(error == 'TimeoutError' and .75 < elapsed < 2.5,
                        'baseline slow-header defect not observed')
        elif case.endswith('-ok'):
            pkg.require(value == b'ok' and error is None and elapsed < .8, 'normal TLS EOF failed')
        else:
            pkg.require(error == 'TimeoutError' and .15 < elapsed < .8, 'absolute TLS deadline failed: '+case)
        rows.append(row)

    # Real TLS deadline failures through the durable task state machine. The
    # peer is a fixture, not a model; capabilities/preflight data are explicit
    # fixtures and are not counted as model acceptance.
    test_tasks.BASE = output
    for phase in ('generation', 'preflight'):
        case = test_tasks.Tasks('test_complete_then_duplicate_is_read_only')
        case.setUp()
        fake = test_tasks.FakeClient()
        requests = []
        with Peer(context, 'headers') as peer:
            network = client_for(transport, profile, peer.port)
            def stopped(body, *, sink):
                requests.append(body)
                return request(network, body, sink)
            if phase == 'generation':
                fake.complete = stopped
                try:
                    case.execute(fake)
                except TimeoutError:
                    pass
                else:
                    raise RuntimeError('real network deadline was not propagated')
                before = case.status()
                quiet = test_tasks.FakeClient()
                pkg.require(case.execute(quiet) == before and quiet.cap_calls == 0 and
                    quiet.calls == [], 'unknown generation replayed')
                expected, generated = 'REQUEST_UNKNOWN', 1
            else:
                fake.preflight = stopped
                before = case.execute(fake)
                expected, generated = 'PREFLIGHT_REJECTED', 0
            pkg.require(before['state'] == expected and before['requests'] == generated and
                len(requests) == 1 and case.count() == 0, 'deadline task state')
        with store.open_task(case.root, case.name) as ledger:
            pkg.save(output/(phase+'-ledger.json'), dict(meta=ledger.meta, events=ledger.events()))
        row = dict(name=phase+'-deadline', state=before, real_tools=0, http_requests=1, **peer.record)
        pkg.save(output/(phase+'-deadline.json'), row)
        rows.append(row)
    pkg.require(len(rows) == 12 and all(row['accepts'] == 1 and row['thread_stopped'] and
        row['listener_released'] for row in rows), 'probe count/cleanup')
    pkg.save(output/'result.json', dict(status='PASS_TLS_DEADLINES', probes=12,
        baseline_transport_sha256=pkg.sha(Path(historical)/'tools/project_tasks/transport.py'),
        candidate_transport_sha256=pkg.sha(Path(transport.__file__)), records=rows,
        model_starts=0, model_requests=0, real_tools=0))
