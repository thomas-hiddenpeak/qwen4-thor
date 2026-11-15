"""Actual native agent CLI, TLS, owned watchdog/restart and private cleanup.

All network peers are local CPU fixtures. No model, GPU, installed service,
external tunnel runtime, or system trust store is touched.
"""
import copy
import http.server
import json
import os
from pathlib import Path
import signal
import re
import ssl
import subprocess
import tempfile
import threading
import time
import unittest

from tools.serve_recovery import test_gateway_nginx as ingress_fixture
from tools.serve_recovery import test_native_access as tls_fixture
from tools.serve_recovery import test_runtime as process_fixture

ROOT = Path(__file__).resolve().parents[2]


class AgentCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ['Q4T_TUNNEL_AGENT_BIN']).resolve(strict=True)
        cls.ingress = Path(os.environ['Q4T_INGRESS']).resolve(strict=True)
        output = Path(os.environ['Q4T_TEST_OUTPUT_DIR']).resolve()
        cls.output = Path(tempfile.mkdtemp(prefix='tunnel-agent-', dir=output))
        cls.pem = cls.output / 'pem'
        tls_fixture.certificates(cls.pem)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix=self._testMethodName+'-', dir=self.output))
        self.pending = []
        self.received = []
        self.private_requests = []
        self.control_headers = []
        self.errors = []
        self.deleted = threading.Event()
        self.result = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.child_ticks = {}
        self.stack = None
        self.servers = []
        self.threads = []
        self.logs = []
        self.addCleanup(self.close)

    def server(self, callback, tls=False):
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_):
                pass
            def do_GET(self):
                callback(self)
            do_POST = do_GET
            do_DELETE = do_GET
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.pem/'server.pem', self.pem/'server.key')
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': 0.02})
        self.servers.append(server)
        self.threads.append(thread)
        thread.start()
        return server.server_port

    @staticmethod
    def reply(handler, code, value=None, headers=None):
        raw = json.dumps(value, ensure_ascii=False).encode() if value is not None else b''
        handler.send_response(code)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(raw)))
        handler.send_header('Connection', 'close')
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        try:
            handler.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass
        handler.close_connection = True

    def control(self, handler):
        headers = {k.lower(): v for k, v in handler.headers.items()}
        with self.lock:
            self.control_headers.append(headers)
        if self.stack:
            if (headers.get('authorization') is not None or
                headers.get('x-q4t-client-id') != 'alpha' or
                headers.get('x-q4t-gateway-token') != self.stack.token):
                self.errors.append('native ingress credential boundary')
        elif headers.get('authorization') != 'Bearer control-secret':
            self.errors.append('control credential')
        if not headers.get('x-tunnel-client-instance-id'):
            self.errors.append('missing instance')
        if handler.command == 'GET':
            with self.lock:
                commands, self.pending = self.pending, []
            self.reply(handler, 200 if commands else 204,
                       dict(commands=commands) if commands else None)
        else:
            if headers.get('x-tunnel-shard-token') != 'original-shard':
                self.errors.append('response shard')
            length = int(handler.headers['Content-Length'])
            if not 0 < length <= 4 << 20:
                self.errors.append('response body size')
                self.reply(handler, 413)
                return
            body = json.loads(handler.rfile.read(length))
            with self.lock:
                self.received.append(body)
            self.reply(handler, 204)
            self.result.set()

    def target(self, handler):
        headers = {k.lower(): v for k, v in handler.headers.items()}
        if headers.get('authorization') != 'Bearer private-target-token':
            self.errors.append('private target credential')
        if 'x-tunnel-shard-token' in headers or 'x-q4t-gateway-token' in headers:
            self.errors.append('control credential leaked to target')
        if handler.command == 'DELETE':
            self.private_requests.append(('DELETE', headers))
            if headers.get('mcp-session-id') != 'private-session':
                self.errors.append('cleanup session identity')
            self.reply(handler, 204)
            self.deleted.set()
            return
        body = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
        self.private_requests.append((body, headers))
        if body['method'] == 'initialize':
            result = dict(protocolVersion='2025-06-18', capabilities={'tools': {}},
                          serverInfo={'name': 'actual-local-mcp', 'version': '1'})
            extra = {'Mcp-Session-Id': 'private-session'}
        else:
            text = (ROOT/'docs/MODEL.md').read_text().splitlines()[0]
            result = dict(content=[dict(type='text', text=text)],
                          structuredContent={'source': 'docs/MODEL.md'}, isError=False)
            extra = None
        self.reply(handler, 200, dict(jsonrpc='2.0', id=body['id'], result=result), extra)

    @staticmethod
    def command(method='tools/call'):
        return dict(request_id='original-request', shard_token='original-shard',
                    command_type='jsonrpc', channel='main', response_timeout='5s',
                    headers={}, jsonrpc=dict(jsonrpc='2.0', id=9007199254740993,
                        method=method, params=dict(name='read_project', arguments={})))

    def config(self, control_port, target_port, tls=False):
        def policy(port):
            return dict(public_networks=False, allowed_networks=['127.0.0.1/32'],
                        ports=[port], allow_http=not tls, timeout_ms=3000,
                        ca_file=str(self.pem/'ca.pem') if tls else '')
        scheme = 'https' if tls else 'http'
        return dict(version=1, api_base=f'{scheme}://127.0.0.1:{control_port}/v1',
                    api_key='control-secret', tunnel_id='tunnel_private',
                    control_fetch=policy(control_port), target_fetch=policy(target_port),
                    target=dict(url=f'{scheme}://127.0.0.1:{target_port}/mcp',
                                authorization='private-target-token'), restart_limit=1)

    def write_config(self, config):
        path = self.work/'agent.json'
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        return path

    def start(self, config):
        path = self.write_config(config)
        out, err = [(self.work/name).open('wb') for name in ('agent.stdout', 'agent.stderr')]
        self.logs.extend((out, err))
        self.process = subprocess.Popen([str(self.binary), 'mcp-tunnel-agent', 'run', str(path)],
                                        stdout=out, stderr=err)

    def wait(self, predicate, timeout=6):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            if predicate():
                return
            if self.process and self.process.poll() is not None:
                self.fail(f'agent exited {self.process.returncode}')
            time.sleep(0.02)
        self.fail('bounded agent fixture deadline')

    def children(self):
        if not self.process or self.process.poll() is not None:
            return []
        owned = []
        # Reuse Thor's established PPID scan: this kernel omits task/children.
        for pid in process_fixture.children(self.process.pid):
            try:
                root = Path(f'/proc/{pid}')
                fields = (root/'stat').read_text().rsplit(')', 1)[1].split()
                if fields[0] == 'Z' or int(fields[1]) != self.process.pid:
                    continue
                command = (root/'cmdline').read_bytes().split(b'\0')
                self.assertIn(b'--mcp-tunnel-agent-worker', command)
                self.child_ticks[pid] = int(fields[19])
                owned.append(pid)
            except (FileNotFoundError, ProcessLookupError):
                continue
        return owned

    def stop(self):
        owned = self.children()
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=5), 0)
        self.assertTrue(all(not Path(f'/proc/{pid}').exists() for pid in owned))
        return owned

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.stack:
            self.stack.close()
        for server, thread in zip(self.servers, self.threads):
            server.shutdown()
            thread.join(3)
            server.server_close()
            self.assertFalse(thread.is_alive())
        for log in self.logs:
            log.close()
        (self.work/'result.json').write_text(json.dumps(dict(
            errors=self.errors, control_requests=len(self.control_headers),
            terminal_responses=len(self.received), private_requests=len(self.private_requests))))
        self.assertEqual(self.errors, [])
        for name in ('agent.stdout', 'agent.stderr'):
            path = self.work/name
            if path.exists():
                raw = path.read_bytes()
                self.assertNotIn(b'control-secret', raw)
                self.assertNotIn(b'private-target-token', raw)

    def test_private_configuration_file_contract_and_redaction(self):
        config = self.config(12345, 12346)
        path = self.write_config(config)
        def check(file, code):
            completed = subprocess.run([str(self.binary), 'mcp-tunnel-agent', 'check', str(file)],
                                       capture_output=True, timeout=5)
            self.assertEqual(completed.returncode, code)
            self.assertNotIn(b'control-secret', completed.stdout+completed.stderr)
            self.assertNotIn(b'private-target-token', completed.stdout+completed.stderr)
            return completed
        value = json.loads(check(path, 0).stdout)
        self.assertFalse(value['model_restart'])
        self.assertFalse(value['external_runtime_required'])
        path.chmod(0o644)
        check(path, 2)
        path.chmod(0o600)
        link = self.work/'link'
        link.symlink_to(path)
        check(link, 2)
        hard = self.work/'hard'
        os.link(path, hard)
        check(path, 2)
        hard.unlink()
        fifo = self.work/'fifo'
        os.mkfifo(fifo, 0o600)
        check(fifo, 2)
        invalid = copy.deepcopy(config)
        invalid['model_restart'] = True
        self.write_config(invalid)
        check(path, 2)

    def test_verified_tls_native_ingress_prefix_and_real_private_document(self):
        context = ssl.create_default_context(cafile=str(self.pem/'ca.pem'))
        self.stack = ingress_fixture.Stack(self.work, native_ingress=self.ingress,
            policy_overrides=dict(version=4, cors=None, http={'path_prefix': '/private'}),
            tls=dict(certificate=str(self.pem/'server.pem'), key=str(self.pem/'server.key'),
                     ca='', require_client_certificate=False), ssl_context=context)
        handler_class = self.stack.servers[0].RequestHandlerClass
        original = handler_class.application
        owner = self
        def application(handler, method):
            if handler.path.startswith('/v1/tunnels/'):
                owner.control(handler)
            else:
                original(handler, method)
        handler_class.application = application
        self.stack.wait(lambda: self.stack.bound == self.stack.generation)
        port = self.stack.value['policy']['listen_port']
        target = self.server(self.target, tls=True)
        config = self.config(port, target, tls=True)
        config.update(api_key='A'*40, api_base=f'https://127.0.0.1:{port}/private/v1')
        self.pending = [self.command()]
        self.start(config)
        self.wait(self.result.is_set)
        self.stop()
        self.assertEqual(len(self.received), 1)
        response = self.received[0]
        self.assertEqual(response['resp_code'], 200)
        self.assertEqual(response['resp_json']['id'], 9007199254740993)
        self.assertEqual(response['resp_json']['result']['content'][0]['text'],
                         (ROOT/'docs/MODEL.md').read_text().splitlines()[0])
        self.assertEqual(len(self.private_requests), 1)
        self.assertEqual(len({h['x-tunnel-client-instance-id'] for h in self.control_headers}), 1)
        self.assertIsNone(self.stack.process.poll())

    def test_owned_watchdog_recovery_and_restart_budget(self):
        control = self.server(self.control)
        target = self.server(self.target)
        self.start(self.config(control, target))
        self.wait(lambda: len(self.control_headers) >= 1 and len(self.children()) == 1)
        original = self.children()[0]
        limits = Path(f'/proc/{original}/limits').read_text()
        address = re.search(r'Max address space\s+(\d+)\s+(\d+)', limits)
        files = re.search(r'Max open files\s+(\d+)\s+(\d+)', limits)
        self.assertEqual(tuple(map(int, address.groups())), (1 << 30, 1 << 30))
        self.assertEqual(tuple(map(int, files.groups())), (32, 32))
        (self.work/'worker.limits').write_text(limits)
        first_instance = self.control_headers[0]['x-tunnel-client-instance-id']
        process_fixture.signal_identity(original, self.child_ticks[original], signal.SIGSTOP)
        started = time.monotonic()
        self.wait(lambda: len(self.children()) == 1 and self.children()[0] != original, 42)
        elapsed = time.monotonic()-started
        self.assertGreater(elapsed, 30)
        self.assertLess(elapsed, 40)
        self.assertFalse(Path(f'/proc/{original}').exists())
        self.wait(lambda: any(h['x-tunnel-client-instance-id'] != first_instance
                             for h in self.control_headers))
        replacement = self.children()[0]
        process_fixture.signal_identity(replacement, self.child_ticks[replacement], signal.SIGKILL)
        self.wait(lambda: not self.children())
        time.sleep(0.4)
        self.assertEqual(self.children(), [])
        self.assertIsNone(self.process.poll())
        self.stop()
        self.assertEqual(self.private_requests, [])
        (self.work/'watchdog.json').write_text(json.dumps(dict(seconds=elapsed,
             owned_restarts=1, new_instance=True, exhausted_budget_held=True)))

    def test_intentional_stop_closes_private_session_without_respawn(self):
        control = self.server(self.control)
        target = self.server(self.target)
        self.pending = [self.command('initialize')]
        self.start(self.config(control, target))
        self.wait(self.result.is_set)
        self.assertEqual(self.received[0]['resp_headers']['mcp-session-id'], ['private-session'])
        self.stop()
        self.assertTrue(self.deleted.wait(1))
        self.assertEqual(len(self.private_requests), 2)
        self.assertEqual(len({h['x-tunnel-client-instance-id'] for h in self.control_headers}), 1)


if __name__ == '__main__':
    unittest.main()
