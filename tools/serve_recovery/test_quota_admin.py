"""Actual C++ policy IPC / quota accounting; CPU fixture, never a model."""
import array
import copy
import ctypes
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import time
import unittest
import uuid

from tools.serve_gateway import client_policy as cp
from tools.serve_recovery import ledger, quota_policy
from tools.serve_recovery.test_gateway import config

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(os.environ.get('Q4T_LIMITS_FIXTURE',
    ROOT / '.q4t-work/serve-access-20261109/host/q4t_client_policy_fixture'))


def receive_line(stream):
    if not select.select([stream], [], [], 3)[0]:
        raise TimeoutError('CPU fixture did not reply')
    line = stream.readline()
    if not line:raise RuntimeError('CPU fixture closed')
    return json.loads(line)


class Server:
    def __init__(self, policy, mode='normal', *, address=None, env=None):
        self.address = address or 'q4t-policy-cpu-' + str(uuid.uuid4())
        self.process = subprocess.Popen([str(FIXTURE), self.address, str(os.getpid()), mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=env)
        self.process.stdin.write(ledger.canonical(policy) + b'\n')
        self.initial = receive_line(self.process.stdout)

    def close(self):
        self.process.stdin.close()
        try:self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill();self.process.wait(timeout=3)
        self.process.stdout.close();self.process.stderr.close()

    def command(self, op, **fields):
        self.process.stdin.write(ledger.canonical(dict(op=op, **fields)) + b'\n')
        return receive_line(self.process.stdout)

    def connection(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        sock.settimeout(3);sock.connect('\0' + self.address)
        return sock

    def request(self, header, raw=None, fragment=16384):
        with self.connection() as sock:
            sock.sendall(ledger.canonical(header))
            if raw is not None:
                for offset in range(0, len(raw), fragment):sock.sendall(raw[offset:offset+fragment])
            return json.loads(sock.recv(8192))

    def publish(self, value, expected, **changes):
        raw = ledger.canonical(value)
        return self.request(dict(op='publish', bytes=len(raw), sha256=cp.digest(value),
            expected_revision=expected, **changes), raw)

    def status(self):return self.request(dict(op='status'))['status']


class QuotaAdminTests(unittest.TestCase):
    def setUp(self):
        self.initial = quota_policy.from_base(config(ROOT)['backend_policy'])
        self.initial['gateway_token'] = 'a'*64
        for client in self.initial['clients']:
            client['requests_per_minute'] = 3;client['tokens_per_minute'] = 100

    def server(self, mode='normal'):
        result = Server(self.initial, mode);self.addCleanup(result.close)
        return result

    def candidate(self, revision=1):
        result = copy.deepcopy(self.initial);result['revision'] = revision
        result['clients'][0]['requests_per_minute'] = 1
        return result

    def test_fragmented_update_preserves_usage_and_survives_management_loss(self):
        server = self.server();pid = server.process.pid
        self.assertTrue(server.command('reserve', id='alpha')['ok'])
        before = server.command('status');self.assertEqual(before['reserved'], 8)
        candidate = self.candidate();raw = ledger.canonical(candidate)
        reply = server.request(dict(op='publish', bytes=len(raw), sha256=cp.digest(candidate),
            expected_revision=0), raw, fragment=17)
        self.assertTrue(reply['ok']);self.assertEqual(reply['status']['revision'], 1)
        self.assertEqual(reply['status']['content_sha256'], cp.digest(candidate))
        self.assertNotIn('a'*64, json.dumps(reply))
        self.assertEqual(server.command('status')['reserved'], 8)
        server.command('settle')
        self.assertFalse(server.command('reserve', id='alpha')['ok'])
        server.command('stop')
        self.assertEqual(server.command('status')['tokens'], 5)
        self.assertFalse(server.command('reserve', id='alpha')['ok'])
        self.assertTrue(server.command('reserve', id='beta')['ok'])
        self.assertIsNone(server.process.poll());self.assertEqual(server.process.pid, pid)

    def test_stale_invalid_capacity_token_and_callback_failures_are_atomic(self):
        server = self.server()
        candidate = self.candidate()
        server.command('lock')
        self.assertFalse(server.publish(candidate, 0)['ok'])
        server.command('unlock')
        for kind in ('token', 'capacity', 'slot', 'fairness', 'expected'):
            bad = copy.deepcopy(candidate)
            if kind == 'token':bad['gateway_token'] = 'b'*64
            if kind == 'capacity':bad['clients'][0]['max_inflight'] = 4096
            if kind == 'slot':bad['clients'].reverse()
            if kind == 'fairness':bad['fair_queue'] = not bad['fair_queue']
            self.assertFalse(server.publish(bad, 1 if kind == 'expected' else 0)['ok'])
            self.assertEqual(server.status()['revision'], 0)
        self.assertTrue(server.publish(candidate, 0)['ok'])
        self.assertFalse(server.publish(candidate, 0)['ok'])
        thrown = self.server('throw')
        with self.assertRaises((json.JSONDecodeError, ConnectionError)):
            thrown.publish(candidate, 0)
        self.assertEqual(thrown.status()['revision'], 0)
        candidate['revision'] = 2
        candidate['clients'].append(dict(candidate['clients'][0], id='gamma'))
        self.assertTrue(thrown.publish(candidate, 0)['ok'])
        self.assertTrue(thrown.command('reserve', id='gamma')['ok'])

    def test_foreign_process_is_readonly_with_actual_peer_credentials(self):
        server = self.server();value = self.candidate()
        code = '''import json,socket,sys
address,header=sys.argv[1:]
for raw in (b'{"op":"status"}', header.encode()):
    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as sock:
        sock.settimeout(3);sock.connect('\\0'+address);sock.sendall(raw)
        print(sock.recv(8192).decode())
'''
        raw = ledger.canonical(value)
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code, server.address,
            json.dumps(dict(op='publish', bytes=len(raw), sha256=cp.digest(value), expected_revision=0))],
            capture_output=True, timeout=5, check=True)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(rows[0]['ok']);self.assertFalse(rows[1]['ok'])
        self.assertEqual(server.status()['revision'], 0)
        self.assertTrue(server.publish(value, 0)['ok'])

    def test_message_bounds_deadlines_and_ancillary_fds_do_not_leak(self):
        server = self.server();candidate = self.candidate();raw = ledger.canonical(candidate)
        fd_count = lambda: len(list(Path('/proc', str(server.process.pid), 'fd').iterdir()))
        baseline = fd_count()
        def check_fds():
            deadline = time.monotonic()+1
            while fd_count() != baseline and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(fd_count(), baseline)
        with open('/dev/null', 'rb') as source:
            for _ in range(5):
                with server.connection() as sock:
                    sock.sendmsg([b'{"op":"status"}'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                        array.array('i', [source.fileno()]))])
                    self.assertEqual(sock.recv(8192), b'')
        check_fds()
        for size in (0, 65537):
            reply = server.request(dict(op='publish', bytes=size, sha256=cp.digest(candidate), expected_revision=0))
            self.assertFalse(reply['ok'])
        for changed in ('0'*64,):
            reply = server.request(dict(op='publish', bytes=len(raw), sha256=changed, expected_revision=0), raw)
            self.assertFalse(reply['ok'])
        with server.connection() as sock:
            sock.sendall(b'x'*16385);self.assertEqual(sock.recv(8192), b'')
        with server.connection() as sock:
            sock.sendall(ledger.canonical(dict(op='publish', bytes=len(raw), sha256=cp.digest(candidate), expected_revision=0)))
            sock.sendall(raw[:16]);started = time.monotonic()
            self.assertEqual(sock.recv(8192), b'')
            self.assertLess(time.monotonic()-started, 2.6)
        self.assertEqual(server.status()['revision'], 0)
        self.assertTrue(server.publish(candidate, 0)['ok'])
        check_fds()

    def test_dead_publisher_cannot_finish_an_inherited_inflight_publication(self):
        # A parent-created connection retains SO_PEERCRED after FD transfer.
        # The pinned pidfd must still reject it after that parent exits.
        libc = ctypes.CDLL(None, use_errno=True);previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        control, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        code = '''import array,json,os,socket,subprocess,sys
fd,fixture,initial,candidate,address=sys.argv[1:]
control=socket.socket(fileno=int(fd));value=json.loads(candidate)
proc=subprocess.Popen([fixture,address,str(os.getpid()),'normal'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
proc.stdin.write(initial.encode()+b'\\n');proc.stdout.readline()
sock=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET);sock.connect('\\0'+address)
raw=value['raw'].encode();sock.sendall(json.dumps(value['header']).encode());sock.sendall(raw[:16])
with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as probe:
    probe.settimeout(3);probe.connect('\\0'+address);probe.sendall(b'{"op":"status"}');probe.recv(8192)
control.sendmsg([json.dumps(dict(pid=proc.pid,address=address)).encode()],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[sock.fileno(),proc.stdin.fileno()]))])
control.recv(1)
'''
        candidate = self.candidate();raw = ledger.canonical(candidate)
        spec = dict(raw=raw.decode(), header=dict(op='publish', bytes=len(raw), sha256=cp.digest(candidate), expected_revision=0))
        owner = None;pid = None;inflight = None;model_input = None
        try:
            owner = subprocess.Popen([sys.executable, '-I', '-B', '-c', code,
                str(endpoint.fileno()), str(FIXTURE), ledger.canonical(self.initial).decode(),
                json.dumps(spec), 'q4t-orphan-policy-'+str(uuid.uuid4())], pass_fds=(endpoint.fileno(),),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            endpoint.close();control.settimeout(3)
            data, ancillary, flags, _ = control.recvmsg(1024, socket.CMSG_SPACE(2*array.array('i').itemsize))
            self.assertFalse(flags);meta = json.loads(data);pid = meta['pid']
            descriptors = array.array('i');descriptors.frombytes(ancillary[0][2])
            self.assertEqual(len(descriptors), 2)
            inflight = socket.socket(fileno=descriptors[0]);inflight.settimeout(3)
            model_input = os.fdopen(descriptors[1], 'wb', buffering=0)
            control.send(b'x');self.assertEqual(owner.wait(timeout=3), 0)
            inflight.sendall(raw[16:]);reply = json.loads(inflight.recv(8192))
            self.assertFalse(reply['ok']);self.assertEqual(reply['status']['revision'], 0)
            self.assertFalse(reply['status']['publisher_alive'])
        finally:
            if inflight is not None:inflight.close()
            if model_input is not None:model_input.close()
            control.close();endpoint.close()
            if owner is not None:
                if owner.poll() is None:owner.kill();owner.wait(timeout=3)
                owner.stderr.close()
            if pid is not None:
                deadline = time.monotonic()+3
                while time.monotonic()<deadline:
                    reaped, status = os.waitpid(pid, os.WNOHANG)
                    if reaped:break
                    time.sleep(.01)
                else:
                    os.kill(pid, 9);os.waitpid(pid, 0)
            libc.prctl(36, previous.value, 0, 0, 0)


if __name__ == '__main__':unittest.main()
