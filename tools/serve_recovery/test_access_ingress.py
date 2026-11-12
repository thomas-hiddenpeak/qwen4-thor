"""Real native credential publication and HTTP with an explicitly CPU backend."""
import copy
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from tools.serve_gateway import gateway
from tools.serve_recovery import access_control, access_policy as policy, journal, ledger
from tools.serve_recovery import test_gateway

ROOT = Path(__file__).resolve().parents[2]
ALPHA, BETA, NEW = 'A' * 40, 'B' * 40, 'C' * 40


class Stack:
    def __init__(self, directory, deferred=True):
        self.directory = directory
        self.address = 'q4t-access-cpu-' + str(uuid.uuid4())
        self.release = threading.Event()
        self.stop = threading.Event()
        self.records = []
        self.native = None
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_args):pass
            def do_GET(self):self.reply()
            def do_POST(self):self.reply()
            def reply(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                row = dict(client=self.headers.get('X-Q4T-Client-Id'),
                    authorization=self.headers.get('Authorization'), path=self.path,
                    token=self.headers.get('X-Q4T-Gateway-Token'))
                owner.records.append(row)
                if raw and json.loads(raw).get('stream'):
                    self.send_response(200);self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Connection', 'close');self.end_headers()
                    self.wfile.write(b'data: ' + json.dumps(row).encode() + b'\n\n');self.wfile.flush()
                    owner.release.wait(6)
                    try:self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
                    except (BrokenPipeError, ConnectionError):pass
                    self.close_connection = True
                else:
                    body = json.dumps(row).encode()
                    self.send_response(200);self.send_header('Content-Length', str(len(body)))
                    self.send_header('Content-Type', 'application/json');self.end_headers()
                    self.wfile.write(body)
        self.servers = [http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) for _ in range(2)]
        for server in self.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        with socket.socket() as choose:
            choose.bind(('127.0.0.1', 0));self.port = choose.getsockname()[1]
        ports = (self.port, *(server.server_port for server in self.servers))
        self.base = gateway.policy(dict(test_gateway.config(directory, ports)['policy'],
            per_client_inflight=1, global_inflight=8, per_client_rps=100, burst=128))
        self.admission = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.admission.bind('\0' + self.address);self.admission.listen(16);self.admission.settimeout(.05)
        def admit():
            while not self.stop.is_set():
                try:peer, _ = self.admission.accept()
                except socket.timeout:continue
                except OSError:return
                with peer:
                    peer.settimeout(1)
                    try:
                        value = json.loads(peer.recv(4096));port = ports[2] if value['lane'] == 'control' else ports[1]
                        peer.send(ledger.canonical(dict(port=port, token='e' * 64)))
                    except (OSError, ValueError, KeyError):pass
        self.admitter = threading.Thread(target=admit, daemon=True);self.admitter.start()
        self.config = dict(version=3 if deferred else 2, parent_pid=os.getpid(),
            parent_start_ticks=journal.process_start(os.getpid()), address=self.address,
            policy=self.base, tls=None, tls_lease='q4t-access-tls-' + str(uuid.uuid4()))
        if deferred:self.config['access_deferred'] = True
        self.policy = policy.from_keys([dict(id='alpha', api_keys=[ALPHA]),
                                        dict(id='beta', api_keys=[BETA])])
        self.publisher = access_control.Publisher(self.address)
        try:
            self.start()
        except BaseException:
            self.close()
            raise

    def start(self):
        path = self.directory / 'ingress.json';path.write_bytes(ledger.canonical(self.config));path.chmod(0o600)
        binary = os.environ.get('Q4T_ACCESS_INGRESS', str(ROOT / '.q4t-work/serve-access-20261109/host/q4t-ingress'))
        self.native = subprocess.Popen([binary, '--config', str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if self.native.poll() is not None:raise RuntimeError('CPU native entrypoint exited')
            try:
                self.request('/livez');return
            except OSError:time.sleep(.02)
        raise TimeoutError('CPU native listener startup')

    def request(self, path='/livez', key=ALPHA, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=4)
        try:
            fields = {'Authorization': 'Bearer ' + key, **(headers or {})}
            c.request('POST' if body is not None else 'GET', path,
                json.dumps(body) if body is not None else None, fields)
            response = c.getresponse();raw = response.read()
            return response.status, json.loads(raw) if raw else None
        finally:c.close()

    def publish(self, value):
        self.publisher.desired(value)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            result = self.publisher.step(self.native.pid)
            if result['live_applied']:
                self.policy = copy.deepcopy(value);return result
            time.sleep(.01)
        raise AssertionError('CPU native policy publication failed: ' + self.publisher.error)

    def next(self, keys):
        return policy.from_keys([dict(id='alpha', api_keys=keys),
                                  dict(id='beta', api_keys=[BETA])], self.policy)

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET);s.settimeout(4)
        s.connect('\0' + self.address + '-access');return s

    def stop_native(self):
        self.publisher.close()
        if self.native is not None and self.native.poll() is None:
            self.native.terminate();self.native.wait(timeout=4)

    def close(self):
        self.release.set();self.stop_native();self.stop.set()
        self.admission.close();self.admitter.join(1)
        for server in self.servers:server.shutdown();server.server_close()


class AccessIngressTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.q4t-work/serve-access-20261109/ingress-fixtures';parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent);self.addCleanup(self.temp.cleanup)
        self.stack = Stack(Path(self.temp.name));self.addCleanup(self.stack.close)

    def test_initial_unavailable_then_complete_authenticated_publication(self):
        self.assertEqual(self.stack.request()[0], 503)
        self.stack.publish(self.stack.policy)
        self.assertEqual(self.stack.request()[0], 200)
        self.assertEqual(self.stack.request(key='invalid')[0], 401)
        status, row = self.stack.request('/v1/chat/completions', body=dict(prompt='CPU fixture'))
        self.assertEqual(status, 200);self.assertEqual(row['client'], 'alpha')
        self.assertIsNone(row['authorization']);self.assertEqual(row['token'], 'e' * 64)

    def test_rotation_keeps_inflight_identity_and_capacity_then_revokes(self):
        self.stack.publish(self.stack.policy)
        c = http.client.HTTPConnection('127.0.0.1', self.stack.port, timeout=4)
        c.request('POST', '/v1/chat/completions', json.dumps(dict(stream=True)),
                  {'Authorization': 'Bearer ' + ALPHA})
        response = c.getresponse();self.assertEqual(response.status, 200)
        self.assertIn(b'"client": "alpha"', response.readline())
        try:
            self.stack.publish(self.stack.next([ALPHA, NEW]))
            self.assertEqual(self.stack.request(key=NEW)[0], 200)
            self.assertEqual(self.stack.request('/v1/chat/completions', key=NEW, body={})[0], 429)
            self.stack.publish(self.stack.next([NEW]))
            self.assertEqual(self.stack.request(key=ALPHA)[0], 401)
            status, row = self.stack.request('/v1/requests/cancel', key=NEW, body={})
            self.assertEqual(status, 200);self.assertEqual(row['client'], 'alpha')
            self.stack.publish(self.stack.next([]))
            self.assertEqual(self.stack.request(key=NEW)[0], 401)
            self.assertEqual(self.stack.request(key=BETA)[0], 200)
            self.stack.release.set();self.assertIn(b'[DONE]', response.read())
        finally:response.close();c.close();self.stack.release.set()

    def test_other_process_can_read_status_but_cannot_publish(self):
        self.stack.publish(self.stack.policy)
        script = '''import json,socket,sys
address='\\0'+sys.argv[1]+'-access'
def call(value):
 with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as s:
  s.settimeout(3);s.connect(address);s.send(json.dumps(value).encode());return json.loads(s.recv(4096))
status=call(dict(op='status'))
publish=call(dict(op='publish',bytes=1,sha256='a'*64,expected_revision=0,initialize=False))
print(json.dumps(dict(read=status['ok'],publish=publish['ok'])))
'''
        r = subprocess.run([sys.executable, '-I', '-B', '-c', script, self.stack.address],
                           capture_output=True, timeout=4, check=True)
        self.assertEqual(json.loads(r.stdout), dict(read=True, publish=False))
        self.assertEqual(self.stack.request()[0], 200)

    def test_large_fragmented_policy_bad_payload_and_slow_peer_do_not_replace(self):
        large = copy.deepcopy(self.stack.policy)
        for i in range(policy.MAX_HISTORY - len(large['key_owners'])):
            large['key_owners'][hashlib.sha256(f'old-key-{i}'.encode()).hexdigest()] = 'alpha'
        self.stack.publish(large)
        with self.stack.connect() as sock:
            sock.send(ledger.canonical(dict(op='publish', bytes=1, sha256='a' * 64,
                expected_revision=0, initialize=False)))
            sock.send(b'{}')
            self.assertFalse(json.loads(sock.recv(4096))['ok'])
        with self.stack.connect() as sock:
            sock.send(ledger.canonical(dict(op='publish', bytes=100, sha256='a' * 64,
                expected_revision=0, initialize=False)))
            sock.send(b'{')
            self.assertEqual(self.stack.request()[0], 200)
            self.assertEqual(sock.recv(4096), b'')
        self.assertEqual(self.stack.request()[0], 200)
        self.assertEqual(self.stack.publisher.status()['desired_sha256'], policy.digest(large))

    def test_new_entrypoint_restores_latest_revocation_without_bootstrap_window(self):
        self.stack.publish(self.stack.policy)
        newest = self.stack.next([]);newest['revision'] = 7
        self.stack.publish(newest)
        self.assertEqual(self.stack.request()[0], 401)
        old_pid = self.stack.native.pid
        self.stack.stop_native();self.stack.start()
        self.assertNotEqual(self.stack.native.pid, old_pid)
        self.assertEqual(self.stack.request()[0], 503)
        self.stack.publish(newest)
        self.assertEqual(self.stack.request()[0], 401)
        self.assertEqual(self.stack.request(key=BETA)[0], 200)
        same_revision = copy.deepcopy(newest)
        same_revision['clients'][0]['key_sha256'] = [policy.key_digest(ALPHA)]
        self.stack.publisher.desired(same_revision)
        end = time.monotonic() + 1
        while time.monotonic() < end:
            result = self.stack.publisher.step(self.stack.native.pid)
            if result['error'] == 'access_durable_revision_conflict':break
            time.sleep(.01)
        self.assertFalse(result['live_applied']);self.assertEqual(self.stack.request()[0], 401)

    def test_legacy_configuration_and_forged_identity_stay_compatible(self):
        self.stack.stop_native();self.stack.config['version'] = 2
        self.stack.config.pop('access_deferred');self.stack.start()
        self.assertEqual(self.stack.request()[0], 200)
        self.assertEqual(self.stack.request(headers={'X-Q4T-Client-Id': 'beta'})[0], 400)
        self.assertEqual(self.stack.request('/v1/chat/completions', key=BETA, body={})[1]['client'], 'beta')

    def test_publisher_does_not_reuse_observation_after_peer_change_or_staleness(self):
        self.stack.publish(self.stack.policy)
        publisher = self.stack.publisher
        original = publisher.clock
        publisher.clock = lambda: original() + 4
        self.assertFalse(publisher.status()['live_applied'])
        self.assertIsNone(publisher.status()['observed'])
        publisher.clock = original
        result = publisher.step(self.stack.native.pid + 1)
        self.assertFalse(result['live_applied']);self.assertIsNone(result['observed'])
        self.assertEqual(self.stack.request()[0], 200)


if __name__ == '__main__':
    unittest.main()
