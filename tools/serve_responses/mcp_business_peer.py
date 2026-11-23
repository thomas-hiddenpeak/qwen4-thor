"""Explicit local test MCP peer: bounded read-only access to real project data."""
import hashlib
import http.server
import json
from pathlib import Path
import secrets
import ssl
import threading
import time

from tools.examples.project_records import ProjectRecords
from tools.serve_service.package import require

DOCUMENT = 'docs/MODEL.md'
URI = 'project://docs/MODEL.md'
ARGUMENTS = dict(path=DOCUMENT, start_line=1, line_count=3)


def catalog():
    return dict(tools=[dict(name='read_project_record',
        description='Read the first three actual project document lines and their citation.',
        inputSchema=dict(type='object', properties=dict(
            path=dict(type='string', const=DOCUMENT), start_line=dict(type='integer', const=1),
            line_count=dict(type='integer', const=3)),
            required=['path', 'start_line', 'line_count'], additionalProperties=False),
        annotations=dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True))])


class DocumentSource:
    name, arguments, uri, filename, mime = (
        'read_project_record', ARGUMENTS, URI, DOCUMENT, 'text/plain')

    def __init__(self, root):
        self.records = ProjectRecords(root)

    def catalog(self):
        return catalog()

    def read(self):
        record = self.records.read(self.arguments)
        require(record['ok'], 'actual project document unavailable')
        return record, dict(uri=self.uri, mimeType=self.mime,
                            text=record['source']['text'])


def prepare_tls(output):
    """Prepare private fixture PKI offline, before sealing a recovery policy."""
    from tools.serve_gateway import tls_fixture
    issuer = tls_fixture.ca(output/'ca')
    now = time.time()
    certificate, key = tls_fixture.issue(output/'leaf', 'localhost', issuer,
        now-30, now+86400, host='localhost')
    return dict(ca_certificate=str(issuer[0]), ca_key=str(issuer[1]),
                certificate=str(certificate), key=str(key))


class ProjectMcp:
    def __init__(self, root, output, port, *, source=None, tls=None,
                 request_metadata=None, required_headers=None, bearer=None,
                 before_reply=None):
        self.source = source if source is not None else DocumentSource(root)
        self.calls, self.reads, self.methods, self.deleted = [], [], [], []
        self.sessions = set()
        self.drop_next = False
        self.http_requests = 0
        self.lock = threading.Lock()
        self.secret = secrets.token_urlsafe(32) if bearer is None else bearer
        material = prepare_tls(output) if tls is None else tls
        require(set(material) == {'ca_certificate', 'ca_key', 'certificate', 'key'},
                'exact private fixture PKI required')
        self.ca = (Path(material['ca_certificate']), Path(material['ca_key']))
        self.certificate, self.key = Path(material['certificate']), Path(material['key'])
        peer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def reply(self, status, value=None, session=None):
                raw = json.dumps(value, ensure_ascii=False).encode() if value is not None else b''
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Connection', 'close')
                if session:
                    self.send_header('MCP-Session-Id', session)
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

            def authorized(self):
                return (self.path == '/mcp' and
                    self.headers.get_all('Authorization') == ['Bearer '+peer.secret])

            def do_DELETE(self):
                if not self.authorized():
                    self.reply(403)
                    return
                session = self.headers.get('MCP-Session-Id')
                with peer.lock:
                    present = session in peer.sessions
                    if present:
                        peer.sessions.remove(session)
                        peer.deleted.append(hashlib.sha256(session.encode()).hexdigest())
                self.reply(204 if present else 404)

            def do_POST(self):
                with peer.lock:
                    peer.http_requests += 1
                if not self.authorized() or any(self.headers.get_all(k) != [v]
                        for k, v in (required_headers or {}).items()):
                    self.reply(403)
                    return
                try:
                    count = int(self.headers['Content-Length'])
                    require(0 < count <= 65536, 'peer body bound')
                    body = json.loads(self.rfile.read(count))
                    require(isinstance(body, dict) and body.get('jsonrpc') == '2.0', 'RPC version')
                    method = body['method']
                    if request_metadata is not None and 'id' in body:
                        require(body['params'].get('_meta') == request_metadata,
                                'peer requires declared MCP request metadata')
                    session = self.headers.get('MCP-Session-Id')
                    with peer.lock:
                        if method != 'initialize' and session not in peer.sessions:
                            self.reply(404)
                            return
                        peer.methods.append(method)
                    if method == 'initialize':
                        session = secrets.token_hex(16)
                        with peer.lock:
                            peer.sessions.add(session)
                        result = dict(protocolVersion='2025-06-18', capabilities=dict(tools={}, resources={}),
                            serverInfo=dict(name='q4t-project-records-validation', version='1'))
                    elif method == 'notifications/initialized':
                        self.reply(202)
                        return
                    elif method == 'tools/list':
                        result = peer.source.catalog()
                    elif method == 'tools/call':
                        params = body['params']
                        require(params.get('name') == peer.source.name and
                                params.get('arguments') == peer.source.arguments,
                                'peer tool/arguments denied')
                        record, _ = peer.source.read()
                        with peer.lock:
                            peer.calls.append(record)
                            drop, peer.drop_next = peer.drop_next, False
                        if drop:
                            # The read actually happened; deliberately lose its receipt.
                            self.close_connection = True
                            return
                        result = dict(content=[dict(type='text', text=json.dumps(record, ensure_ascii=False)),
                            dict(type='resource_link', uri=peer.source.uri,
                                name=peer.source.filename, mimeType=peer.source.mime)],
                            structuredContent=record, isError=False)
                    elif method == 'resources/read':
                        require({k:v for k,v in body['params'].items() if k != '_meta'} == dict(uri=peer.source.uri),
                                'peer resource denied')
                        record, content = peer.source.read()
                        with peer.lock:
                            peer.reads.append(record['source'])
                        result = dict(contents=[content])
                    else:
                        self.reply(400)
                        return
                    if before_reply is not None:
                        before_reply(method)
                    self.reply(200, dict(jsonrpc='2.0', id=body['id'], result=result),
                               session if method == 'initialize' else None)
                except (ValueError, KeyError, TypeError, RuntimeError):
                    self.reply(400)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.certificate, self.key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.02})
        self.thread.start()

    @property
    def url(self):
        return f'https://localhost:{self.server.server_port}/mcp'

    def fetch(self):
        return dict(public_networks=False, allow_http=False,
            allowed_networks=['127.0.0.1/32', '::1/128'], ports=[self.server.server_port],
            ca_file=str(self.ca[0]), timeout_ms=5000, max_redirects=0)

    def snapshot(self):
        with self.lock:
            return dict(calls=list(self.calls), resource_reads=list(self.reads),
                methods=list(self.methods), deleted_sessions=len(self.deleted), live_sessions=len(self.sessions))

    def close(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        require(not self.thread.is_alive(), 'private MCP peer did not stop')
