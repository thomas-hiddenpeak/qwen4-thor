"""Actual loopback HTTP/TLS peers, called only inside the full-stage GPU lease."""
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time

from tools.examples import chat_client
from tools.serve_service import package as pkg

ROOT = Path(__file__).resolve().parents[2]
MODEL_LIST = dict(object='list', data=[dict(id='qwen3.8-flash-next',
    object='model', created=42, owned_by='q4t')])
BODY = json.dumps(MODEL_LIST).encode()
SLOW = ('handshake', 'status', 'headers', 'interim', 'body',
        'error-body', 'chunk-size', 'trailer')
CASES = (*SLOW, 'length-ok', 'chunk-ok', 'close-ok', 'truncated-length',
         'truncated-trailer', 'redirect', 'wrong-ca', 'wrong-host', 'localhost-dns')


class Peer:
    def __init__(self, context, case):
        self.context, self.case = context, case
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(2)
        self.listener.settimeout(2)
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        self.record = dict(case=case, accepts=0, requests=0, sent_bytes=0)
        self.thread = threading.Thread(target=self.serve)

    def send(self, peer, value, slow=False):
        for part in ([bytes([c]) for c in value] if slow else [value]):
            if self.stop.is_set():
                return
            peer.sendall(part)
            self.record['sent_bytes'] += len(part)
            if slow and self.stop.wait(.03):
                return

    def respond(self, peer):
        header = bytearray()
        while b'\r\n\r\n' not in header:
            part = peer.recv(4096)
            if not part:
                return
            header.extend(part)
            pkg.require(len(header) <= 65536, 'peer header bound')
        self.record['requests'] += 1
        fields = header.split(b'\r\n\r\n', 1)[0].decode('ascii').lower()
        lengths = [line.split(':', 1)[1].strip() for line in fields.split('\r\n')
                   if line.startswith('content-length:')]
        pkg.require(len(lengths) <= 1, 'duplicate request length')
        length = int(lengths[0]) if lengths else 0
        pkg.require(0 <= length <= 65536, 'peer body bound')
        body = header.split(b'\r\n\r\n', 1)[1]
        while len(body) < length:
            part = peer.recv(min(4096, length-len(body)))
            pkg.require(bool(part), 'incomplete probe request')
            body.extend(part)
        self.record['request_body_bytes'] = len(body)
        self.record['key_received'] = '\r\nauthorization: bearer transport-probe-key' in fields
        self.record['host_received'] = '\r\nhost: ' in fields
        if self.case == 'interim':
            self.send(peer, b'HTTP/1.1 100 Continue\r\n\r\n'*8, True)
            return
        if self.case == 'redirect':
            self.send(peer, b'HTTP/1.1 302 Found\r\nLocation: /v1/redirect-target\r\n'
                           b'Content-Length: 0\r\nConnection: close\r\n\r\n')
            return
        self.send(peer, b'HTTP/1.1 503 Busy\r\n' if self.case == 'error-body'
                  else b'HTTP/1.1 200 OK\r\n', self.case == 'status')
        if self.case == 'headers':
            self.send(peer, b'X-Slow: '+b'x'*40+b'\r\n', True)
        self.send(peer, b'Content-Type: application/json\r\nConnection: close\r\n')
        if self.case in ('chunk-size', 'trailer', 'chunk-ok', 'truncated-trailer'):
            self.send(peer, b'Transfer-Encoding: chunked\r\n\r\n')
            if self.case == 'chunk-size':
                self.send(peer, b'1'*40+b'\r\n', True)
                return
            self.send(peer, hex(len(BODY))[2:].encode()+b'\r\n'+BODY+b'\r\n0\r\n')
            if self.case == 'trailer':
                self.send(peer, b'X-Slow: '+b'x'*40+b'\r\n', True)
            if self.case != 'truncated-trailer':
                self.send(peer, b'\r\n')
        elif self.case == 'close-ok':
            self.send(peer, b'\r\n'+BODY)
        else:
            length = len(BODY)+(7 if self.case == 'truncated-length' else 0)
            self.send(peer, b'Content-Length: '+str(length).encode()+b'\r\n\r\n')
            self.send(peer, BODY, self.case in ('body', 'error-body'))

    def serve(self):
        try:
            with self.listener.accept()[0] as raw:
                self.record['accepts'] += 1
                raw.settimeout(2)
                if self.case == 'handshake':
                    self.stop.wait(2)
                elif self.case == 'localhost-dns':
                    self.respond(raw)
                else:
                    with self.context.wrap_socket(raw, server_side=True) as peer:
                        self.respond(peer)
        except (OSError, ssl.SSLError) as error:
            self.record['peer_error'] = type(error).__name__
        except BaseException as error:
            self.record['unexpected_error'] = type(error).__name__
        finally:
            self.listener.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.listener.close()
        self.thread.join(3)
        pkg.require(not self.thread.is_alive() and 'unexpected_error' not in self.record,
                    'local peer cleanup failed')
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', self.port))
        self.record.update(thread_stopped=True, listener_released=True)


def python_request(options):
    client = chat_client.ChatClient(options['url'], timeout=.25,
        api_key='transport-probe-key', ca_file=options.get('ca'),
        connect_host=options.get('connect'))
    began = time.monotonic()
    try:
        with client._post('/probe', {}, .25) as response:
            value = json.loads(response.read(4096))
        row = dict(ok=True, models=value, status=200)
    except Exception as error:
        cause = getattr(error, 'reason', error)
        row = dict(ok=False, error=type(error).__name__, cause=type(cause).__name__,
                   status=getattr(error, 'status', None))
    row['seconds'] = time.monotonic()-began
    return row


def execute(output, cert, key, wrong_ca, sdk_directory, budget, reused=None):
    output.mkdir()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    env = {k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
    env['Q4T_NODE_SDK_DIR'] = sdk_directory
    rows = []
    for language in ('python', 'node'):
        for case in CASES:
            name = language+'-'+case
            if reused and name in reused:
                row = pkg.read(reused[name])
                pkg.require(case in SLOW and language == 'python' and not row['ok'] and
                            .15 < row['seconds'] < 1.5 and row['thread_stopped'] and
                            row['listener_released'], 'invalid reused deadline observation')
                pkg.save(output/(name+'.json'), row)
                rows.append(row)
                continue
            budget('network')
            with Peer(context, case) as peer:
                options = dict(url=f'https://q4t.internal.test:{peer.port}/v1',
                               ca=str(wrong_ca if case == 'wrong-ca' else cert), connect='127.0.0.1')
                if case == 'wrong-host':
                    options['url'] = f'https://wrong.internal.test:{peer.port}/v1'
                if case == 'localhost-dns':
                    options = dict(url=f'http://localhost:{peer.port}/v1')
                if language == 'python':
                    row = python_request(options)
                else:
                    child = subprocess.run(['node', str(Path(__file__).with_name('probe.mjs'))],
                        input=json.dumps(options), text=True, capture_output=True, env=env, timeout=5)
                    pkg.require(child.returncode == 0 and not child.stderr, 'Node peer driver failed')
                    row = json.loads(child.stdout)
            row.update(name=name, **peer.record)
            pkg.save(output/(name+'.json'), row)
            rows.append(row)
            pkg.require(row['accepts'] == 1 and row['requests'] <= 1, 'probe retried or not connected: '+name)
            if row['requests']:
                pkg.require(row['key_received'] and row['host_received'], 'connection header mismatch: '+name)
            if case in ('length-ok', 'chunk-ok', 'close-ok', 'localhost-dns'):
                pkg.require(row['ok'] and row['models'] == MODEL_LIST, 'normal framed response failed: '+name)
            else:
                pkg.require(not row['ok'], 'invalid response accepted: '+name)
                if case in SLOW:
                    pkg.require(.15 < row['seconds'] < 1.5, 'network deadline escaped: '+name)
                if case == 'redirect':
                    pkg.require(row['status'] == 302, 'redirect handling changed')
    result = dict(passed=True, cases=len(rows), rows=rows, models=0)
    pkg.save(output/'result.json', result)
    return result
