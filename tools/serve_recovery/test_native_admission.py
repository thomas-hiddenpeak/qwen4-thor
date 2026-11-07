"""Native HTTP boundary races against a controlled, PID-bound CPU IPC peer."""
import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_gateway import gateway as files
from tools.serve_recovery import journal
from tools.serve_recovery.native_ingress import AdmissionSocket
from tools.serve_recovery.test_gateway import config
from tools.serve_recovery.test_gateway_nginx import free_port

INGRESS = None


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        output = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR', ROOT/'build/native-admission'))
        output.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix='admission-', dir=output))
        self.forwarded = []
        self.threads, self.backends = [], []
        owner = self
        class Backend(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                owner.forwarded.append(raw)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', '2')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(b'{}')
        for _ in range(2):
            server = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
            server.daemon_threads = True
            self.backends.append(server)
            self.thread(server.serve_forever)
        self.port = free_port()
        self.value = config(self.directory,
            ports=(self.port, *(server.server_port for server in self.backends)))
        self.address = f'q4t-ingress-{os.geteuid()}-{uuid.uuid4()}'
        self.decision = dict(port=self.backends[0].server_port, token='e'*64)
        self.admitted = threading.Event()
        self.stopped = threading.Event()
        self.process = None
        self.calls = 0
        def decide(_lane):
            result = dict(self.decision)
            self.calls += 1
            self.admitted.set()
            return result
        self.server = AdmissionSocket(self.address, ('alpha', 'beta'), decide,
            lambda: self.process.pid if self.process is not None else None)
        def serve():
            while not self.stopped.is_set():
                self.server.step(0.01)
        self.thread(serve)
        path = self.directory/'ingress.json'
        files.save(path, json.dumps(dict(version=1, parent_pid=os.getpid(),
            parent_start_ticks=journal.process_start(os.getpid()), address=self.address,
            policy=files.policy(self.value['policy']))).encode())
        self.log = (self.directory/'native.log').open('wb')
        self.process = subprocess.Popen([str(INGRESS), '--config', str(path)],
            env=dict(PATH='/no-nginx-or-systemd', LANG='C.UTF-8'),
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log)
        end = time.monotonic()+5
        while time.monotonic() < end:
            self.assertIsNone(self.process.poll())
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.01)
        else:
            self.fail('native listener did not start')

    def thread(self, target):
        worker = threading.Thread(target=target, daemon=True)
        self.threads.append(worker)
        worker.start()

    def tearDown(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=3)
        self.stopped.set()
        for server in self.backends:
            server.shutdown()
            server.server_close()
        for worker in self.threads:
            worker.join(timeout=2)
        self.server.close()
        self.log.close()
        (self.directory/'result.json').write_text(json.dumps(dict(
            admission_calls=self.calls, forwarded=len(self.forwarded), cpu_fixture=True)))

    def pending(self):
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=3)
        sock.sendall(('POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n'
            'Authorization: Bearer '+'A'*40+'\r\nContent-Length: 2\r\n\r\n').encode())
        self.assertTrue(self.admitted.wait(2))
        return sock

    def test_generation_change_while_reading_body_never_forwards_to_successor(self):
        with self.pending() as sock:
            self.decision = dict(self.decision, token='f'*64)
            sock.sendall(b'{}')
            response = http.client.HTTPResponse(sock)
            response.begin()
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(response.read())['error']['code'], 'service_recovering')
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.forwarded, [])

    def test_quota_wait_after_headers_still_blocks_dispatch(self):
        with self.pending() as sock:
            self.decision = dict(error='service_quota_wait')
            sock.sendall(b'{}')
            response = http.client.HTTPResponse(sock)
            response.begin()
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(response.read())['error']['code'], 'service_quota_wait')
        self.assertEqual(self.forwarded, [])

    def test_same_uid_unowned_process_cannot_read_internal_credentials(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
            sock.settimeout(2)
            sock.connect('\0'+self.address)
            try:
                sock.send(b'{"version":1,"client":"alpha","lane":"data"}')
                raw = sock.recv(4096)
            except (ConnectionResetError, BrokenPipeError):
                raw = b''
            self.assertEqual(raw, b'')
        self.assertEqual(self.calls, 0)

    def test_stop_wakes_incomplete_body_without_waiting_for_read_deadline(self):
        with self.pending():
            start = time.monotonic()
            self.process.send_signal(signal.SIGTERM)
            self.assertEqual(self.process.wait(timeout=3), 0)
            self.assertLess(time.monotonic()-start, 3)
        self.assertEqual(self.forwarded, [])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--ingress', type=Path, required=True)
    args, rest = parser.parse_known_args()
    INGRESS = args.ingress.resolve(strict=True)
    unittest.main(argv=[sys.argv[0], *rest])
