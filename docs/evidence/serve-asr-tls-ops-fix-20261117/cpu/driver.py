import ast
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import runpy
import threading
import time
import traceback
from types import SimpleNamespace

root = Path.cwd()
work = root/'.q4t-work/serve-audio-native-20261117'
out = work/'ops-driver-cpu-01'
out.mkdir()
names = ['tools/native_asr/service_acceptance.py', 'tools/native_asr/tls_composition.py']
sources = {}
for name in names:
    path = root/name; sources[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    target = out/'source'/name; target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(path.read_bytes()); ast.parse(path.read_text())
plan = dict(tests=['private-config-port-and-error-contract', 'completed-real-tls-composition'],
            seconds=60, gpu_actions=0, sources=sources)
(out/'plan.json').write_text(json.dumps(plan, indent=2)+'\n')
started = time.monotonic()
result = dict(status='FAILED', tests=[], gpu_actions=0)
try:
    driver = runpy.run_path(names[0]); requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            requests.append(self.path)
            body = b'{"native_asr_health":{"state":"ready"}}'
            self.send_response(200 if len(requests) == 1 else 404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
    server = HTTPServer(('127.0.0.1', 0), Handler)
    server.timeout = 3
    def serve():
        for _ in range(2):
            server.handle_request()
    worker = threading.Thread(target=serve)
    worker.start()
    stage = driver['Stage'].__new__(driver['Stage'])
    calls = []
    stage.budget = SimpleNamespace(reserve=lambda *args:calls.append(args))
    stage.policy = dict(listen_port=1, upstream_operations_port=server.server_port)
    stage.output = out/'requests'; stage.output.mkdir()
    try:
        stage.configuration()
        raw = json.loads((stage.output/'final-config.wire').read_text())
        assert raw['native_asr_health']['state'] == 'ready'
        try:
            stage.configuration()
        except RuntimeError as error:
            assert str(error) == 'private configuration unavailable'
        else:
            raise AssertionError('private config error accepted')
    finally:
        worker.join(7); server.server_close()
    assert not worker.is_alive() and requests == ['/v1/config', '/v1/config']
    assert calls == [('controls', 'final-config')]*2
    result['tests'].append(dict(name=plan['tests'][0], status='PASS', negative_controls=1))
    audit = runpy.run_path(names[1])
    review = audit['review'](work/'native-tls-02')
    assert review['status'] == 'TLS_BUSINESS_VERIFIED_FINAL_PRIVATE_HEALTH_PENDING'
    assert review['counts'] == dict(starts=2, chats=8, probes=0)
    assert not review['final_tls_private_health_observed']
    (out/'review.json').write_text(json.dumps(review, indent=2)+'\n')
    result['tests'].append(dict(name=plan['tests'][1], status='PASS',
                                original_driver_statuses=['FAILED', 'FAILED']))
    result['status'] = 'PASS'
except BaseException:
    result['error'] = traceback.format_exc()
result['seconds'] = time.monotonic()-started
if result['seconds'] > 60:
    result['status'] = 'FAILED'
(out/'result.json').write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps(result))
raise SystemExit(0 if result['status'] == 'PASS' else 1)
