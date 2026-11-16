"""Native isolated MCP HTTPS, real local document result, and CA/name refusal."""
import http.server
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]


class NativeMcpTls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ['Q4T_MCP_TEST_BIN']).resolve()
        output = Path(os.environ['Q4T_TEST_OUTPUT_DIR']).resolve()
        output.mkdir(parents=True, exist_ok=True)
        cls.work = Path(tempfile.mkdtemp(prefix='mcp-tls-', dir=output))
        cls.cert, cls.key = cls.work / 'ca.pem', cls.work / 'key.pem'
        done = subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(cls.key), '-out', str(cls.cert), '-days', '1',
            '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
            capture_output=True, timeout=15)
        (cls.work / 'certificate.stdout').write_bytes(done.stdout)
        (cls.work / 'certificate.stderr').write_bytes(done.stderr)
        if done.returncode:
            raise RuntimeError('fixture certificate generation failed')

    def setUp(self):
        self.requests = []
        self.reads = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def send_result(self, status, result=None):
                body = json.dumps(result).encode() if result is not None else b''
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                if length < 1 or length > 1 << 20:
                    self.send_result(413)
                    return
                request = json.loads(self.rfile.read(length))
                owner.requests.append((request, dict(self.headers)))
                if self.headers.get('Authorization') != 'Bearer fixture-mcp-token':
                    self.send_result(401)
                    return
                method = request.get('method')
                if method == 'initialize':
                    result = dict(protocolVersion='2025-06-18',
                                  capabilities={'tools': {}},
                                  serverInfo={'name': 'project-fixture', 'version': '1'})
                elif method == 'notifications/initialized':
                    self.send_result(202)
                    return
                elif method == 'tools/call':
                    if request['params'] != dict(name='read_project',
                                                arguments={'path': 'docs/MODEL.md'}):
                        self.send_result(400)
                        return
                    text = (ROOT / 'docs/MODEL.md').read_text().splitlines()[0]
                    owner.reads.append('docs/MODEL.md')
                    result = dict(content=[{'type': 'text', 'text': text}],
                                  structuredContent={'source': 'docs/MODEL.md'},
                                  isError=False)
                else:
                    self.send_result(400)
                    return
                self.send_result(200, dict(jsonrpc='2.0', id=request['id'], result=result))

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.02})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive())

    def invoke(self, name, host='localhost', ca=None):
        port = self.server.server_port
        policy = dict(public_networks=False, allow_http=False,
                      allowed_networks=['127.0.0.1/32', '::1/128'], ports=[port],
                      ca_file=str(self.cert) if ca is None else ca, timeout_ms=2000)
        result = subprocess.run([str(self.binary), '--mcp-transport-fixture',
                                 f'https://{host}:{port}/mcp', json.dumps(policy)],
                                capture_output=True, timeout=8)
        (self.work / f'{name}.stdout').write_bytes(result.stdout)
        (self.work / f'{name}.stderr').write_bytes(result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        wire = json.loads(result.stdout)
        self.assertEqual((wire['started'], wire['reaped'], wire['active']), (1, 1, 0))
        self.assertNotIn(b'fixture-mcp-token', result.stdout + result.stderr)
        return wire

    def test_verified_tls_reads_actual_project_document_once(self):
        result = self.invoke('valid')
        self.assertEqual(result['status'], 200)
        self.assertTrue(result['permitted'])
        self.assertFalse(result['unknown'])
        self.assertEqual(result['result']['content'][0]['text'],
                         (ROOT / 'docs/MODEL.md').read_text().splitlines()[0])
        self.assertEqual(self.reads, ['docs/MODEL.md'])
        self.assertEqual([r[0]['method'] for r in self.requests],
                         ['initialize', 'notifications/initialized', 'tools/call'])
        self.assertEqual(self.requests[-1][1]['MCP-Protocol-Version'], '2025-06-18')

    def test_untrusted_or_wrong_name_never_sends_credentials_or_tool(self):
        for name, kwargs in [('wrong-name', {'host': '127.0.0.1'}),
                             ('untrusted', {'ca': ''})]:
            with self.subTest(name=name):
                result = self.invoke(name, **kwargs)
                self.assertEqual((result['status'], result['code']),
                                 (502, 'mcp_fetch_tls_failed'))
                self.assertFalse(result['permitted'])
                self.assertEqual(self.requests, [])
                self.assertEqual(self.reads, [])


if __name__ == '__main__':
    unittest.main()
