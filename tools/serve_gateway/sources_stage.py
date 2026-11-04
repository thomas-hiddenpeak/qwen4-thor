"""One-lease, source-bound TLS checks and two real project tool round trips."""
import argparse
import copy
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_gateway import sources_cpu as cpu, managed as m, tls
from tools.serve_clients import short as s
from tools.serve_error_params.stage import Children, h, DAYS
from tools.serve_service import package as pkg

BASE = cpu.BASE
PLAN, OUT = BASE/'plan.json', BASE/'execution'
PORT, TLS_PORT = 18131, 18451
LIMITS = dict(starts=1, generation_attempts=7, controls=80, seconds=240)
FORWARDED = {'X-Forwarded-For': '127.0.0.1', 'X-Real-IP': '127.0.0.1',
             'Forwarded': 'for=127.0.0.1;proto=https'}


def matrix():
    rows = []
    def add(name, path='/v1/models', method='GET', body=None, **kw):
        rows.append(dict(name=name, path=path, method=method, body=body,
            source='127.0.0.2', client=0, headers={}, expected=403,
            code='gateway_source_denied', **kw))
    add('models')
    add('capabilities', '/v1/capabilities')
    request = dict(model='qwen3.8-flash-next', messages=[dict(role='user', content='1')],
                   max_tokens=1)
    add('completion', '/v1/chat/completions', 'POST', request)
    add('preflight', '/v1/chat/preflight', 'POST', dict(request=request))
    add('cancel', '/v1/requests/cancel', 'POST', dict(request_id='not-active', cancel_token='a'*64))
    for name, headers in [('xff', {'X-Forwarded-For': '127.0.0.1'}),
                          ('xreal', {'X-Real-IP': '127.0.0.1'}),
                          ('forwarded', {'Forwarded': FORWARDED['Forwarded']}),
                          ('combined', FORWARDED)]:
        add(name); rows[-1]['headers'] = headers
    add('no-key'); rows[-1]['client'] = None
    add('bad-key'); rows[-1]['client'] = 'invalid'
    add('admin', '/metrics')
    add('malformed-json', '/v1/chat/completions', 'POST', '{')
    add('headers-only', '/v1/chat/completions', 'POST')
    rows[-1]['headers'] = {'Content-Length': '65536'}
    add('wrong-host'); rows[-1]['headers'] = {'Host': 'wrong.internal.test'}
    add('second-peer'); rows[-1].update(source='127.0.0.3', headers=FORWARDED)
    allowed = {'X-Forwarded-For': '127.0.0.2', 'X-Real-IP': '127.0.0.2',
               'Forwarded': 'for=127.0.0.2;proto=https'}
    for name, client, path, status, code in (
        ('allowed-forged', 0, '/v1/models', 200, None),
        ('allowed-no-key', None, '/v1/models', 401, 'gateway_unauthorized'),
        ('allowed-bad-key', 'invalid', '/v1/models', 401, 'gateway_unauthorized'),
        ('allowed-admin', 0, '/metrics', 404, 'gateway_route_not_found'),
        ('allowed-beta-budget', 1, '/v1/chat/preflight', 400, 'client_output_limit_exceeded')):
        body = None if status != 400 else dict(request=dict(request, max_tokens=65))
        rows.append(dict(name=name, source='127.0.0.1', client=client, path=path,
            method='GET' if body is None else 'POST', body=body, headers=allowed,
            expected=status, code=code))
    pkg.require(len(rows) == 21 and sum(r['expected'] == 403 for r in rows) == 16,
                'fixed source matrix')
    return rows


class Connection(http.client.HTTPSConnection):
    def connect(self):
        raw = socket.create_connection(('127.0.0.1', self.port), self.timeout,
                                       source_address=self.source_address)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve prior acceptance')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                'commit reviewed stage before freeze')
    tested = pkg.read(BASE/'cpu-01/result.json')
    fixed = pkg.read(BASE/'cpu-syntax-fix/result.json')
    pkg.require(not tested['passed'] and tested['tests_run'] == 78 and
                tested['errors'] == 1 and tested['failures'] == 0 and
                fixed['passed'] and fixed['tests_run'] == 7, 'CPU original/correction gate')
    repaired = {'tools/serve_gateway/test_sources.py', 'tools/serve_gateway/sources_stage.py'}
    pkg.require({name for name, digest in tested['sources'].items()
                 if pkg.sha(ROOT/name) != digest} == repaired, 'unexpected CPU repair delta')
    for name, digest in fixed['sources'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'tested source changed: '+name)
    private = BASE/'private'
    private.mkdir(mode=0o700)
    cert, key = s.certificate(private)
    old = pkg.read(ROOT/'.q4t-work/serve-task-protocol-20261104/retry-01/plan.json')
    original = ROOT/'.q4t-work/serve-clients-managed-20261104/model-private'
    front = pkg.read(original/'policy-private.json')
    front.update(version=2, listen_host='127.0.0.1', listen_port=TLS_PORT,
        upstream_port=PORT, allowed_client_cidrs=['127.0.0.1/32'],
        per_client_rps=100, burst=128)
    s.g.save(private/'gateway.json', json.dumps(front).encode())
    s.g.save(private/'clients.json', (original/'clients.json').read_bytes())
    outer = m.prepare(private/'gateway.json', pkg.read(s.VENDOR)['binary'], cert, key,
        BASE/'gateway', private/'clients.json', trusted_ca=cert)
    manifest, _ = m.verify(outer['package'], outer['manifest_sha256'])
    release = pkg.read(ROOT/'build/q4t.release.json')
    runtime = pkg.read(release['tested_source_manifest'])
    pkg.require(all(pkg.sha(ROOT/n) == digest for n, digest in runtime.items()),
                'default runtime drift')
    source_files = dict(fixed['sources'])
    source_files.update({name: pkg.sha(ROOT/name) for name in cpu.sources()})
    source_files.update({str(path.relative_to(ROOT)): pkg.sha(path) for path in
                        (ROOT/'docs').glob('serve-*.json')})
    source_files['docs/MODEL.md'] = pkg.sha(ROOT/'docs/MODEL.md')
    for name in source_files:
        path = BASE/'source'/name
        path.parent.mkdir(parents=True, exist_ok=True)
        s.g.save(path, (ROOT/name).read_bytes())
    value = dict(root=str(ROOT), limits=LIMITS, matrix=matrix(), port=PORT, tls_port=TLS_PORT,
        binary=str(ROOT/'build/q4t'), protocol=release['chat_protocol']['protocol_sha256'],
        model_dir=old['model_dir'], model_inputs=old['model_inputs'],
        frontend=str(private/'gateway.json'), policy=str(private/'clients.json'),
        gateway=manifest['gateway'], managed_gateway=outer, source_files=source_files,
        runtime=runtime, preserved=old['preserved'], days=list(map(str, DAYS)),
        default={str(ROOT/name): pkg.sha(ROOT/name) for name in ('build/q4t', 'build/q4t.release.json')},
        local={str(path): pkg.sha(path) for path in
               (private/'gateway.json', private/'clients.json', BASE/'cpu-01/result.json',
                BASE/'cpu-syntax-fix/result.json')},
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True, cwd=ROOT).strip(),
        long_run=False, performance_acceptance=False, new_lan_listener=False,
        planned=dict(models=1, gateways=1, tls_probes=21, source_refusals=16,
                     complete_responses=4, real_tools=2))
    pkg.save(PLAN, value)
    check()
    return dict(prepared=True, models_started=0, listeners_started=0, limits=LIMITS)


def check():
    plan = pkg.read(PLAN)
    pkg.require(plan['root'] == str(ROOT) and plan['limits'] == LIMITS and
                plan['matrix'] == matrix() and (plan['port'], plan['tls_port']) == (PORT, TLS_PORT),
                'frozen scope changed')
    for name, digest in {**plan['source_files'], **plan['runtime']}.items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'source changed: '+name)
    for name, digest in {**plan['preserved'], **plan['default'], **plan['local']}.items():
        pkg.require(pkg.sha(name) == digest, 'fixed input changed')
    for path in plan['days']:
        pkg.require(not (Path(path).parent/'execution').exists(), 'user day started')
    for name, digest in plan['model_inputs'].items():
        pkg.require(pkg.sha(Path(plan['model_dir'])/name) == digest, 'model identity changed')
    outer, inner = m.verify(plan['managed_gateway']['package'], plan['managed_gateway']['manifest_sha256'])
    pkg.require(outer['gateway'] == plan['gateway'] and inner['policy']['version'] == 2 and
                inner['policy']['allowed_client_cidrs'] == ['127.0.0.1/32'] and
                tls.observe(inner['tls'])['remaining_seconds'] > LIMITS['seconds']+3600,
                'prepared gateway/source/TLS identity')
    return plan


class Run(s.Run):
    def __init__(self, plan):
        h.require_guard(OUT)
        s.OUT, s.PORT, s.TLS_PORT, s.LIMITS = OUT/'business', PORT, TLS_PORT, LIMITS
        s.OUT.mkdir(mode=0o700)
        super().__init__(plan)
        self.children = Children(OUT/'lease')
        self.current_client = 0
        self.probes = []

    def begin(self, label, body=None, **kwargs):
        if not kwargs.get('direct'):
            kwargs['headers'] = dict(matrix()[16]['headers'], **kwargs.get('headers', {}))
        return super().begin(label, body, **kwargs)

    def complete(self, label, body, client=None):
        client = self.current_client if client is None else client
        body = copy.deepcopy(body)
        body['max_tokens'] = min(body.get('max_tokens', 512), 512 if client == 0 else 64)
        return super().complete(label, body, client)

    def probe(self, row):
        self.budget('generation_attempts' if row['path'] == '/v1/chat/completions' else 'controls')
        label = 'source-'+row['name']
        connection = Connection(self.front['server_name'], TLS_PORT, context=self.context,
                                timeout=8, source_address=(row['source'], 0))
        self.sockets.append(connection)
        started = time.monotonic()
        fields = {'Content-Type': 'application/json', 'Connection': 'close'}
        if row['client'] is not None:
            token = ('invalid-source-probe-key' if row['client'] == 'invalid' else
                     self.front['clients'][row['client']]['api_key'])
            fields['Authorization'] = 'Bearer '+token
        fields.update(row['headers'])
        raw_body = row['body']
        if isinstance(raw_body, dict):
            raw_body = json.dumps(raw_body)
        pkg.save(s.OUT/(label+'-request.json'), row)
        try:
            connection.connect()
            observed = dict(source=connection.sock.getsockname()[0],
                leaf_sha256=tls.sha(connection.sock.getpeercert(binary_form=True)),
                tls_version=connection.sock.version(), verified=True)
            connection.request(row['method'], row['path'], raw_body, fields)
            response = connection.getresponse()
            headers = {k.lower(): v for k, v in response.getheaders()}
            raw = self.record(label, response, response.read(65537), row['expected'])
            pkg.require(len(raw) <= 65536 and observed['source'] == row['source'], 'probe body/source bound')
            value = json.loads(raw)
            if row['code'] is not None:
                pkg.require(value['error']['code'] == row['code'], 'source/auth/budget error: '+label)
            pkg.require(re.fullmatch(r'[a-f0-9]{32}', headers.get('x-q4t-gateway-request-id', '')),
                        'gateway correlation id')
            if row['expected'] == 403:
                pkg.require(value['error']['type'] == 'gateway_error' and value['error']['param'] is None and
                    headers.get('cache-control') == 'no-store' and 'x-request-id' not in headers,
                    'source refusal crossed upstream/framing boundary')
            elapsed = time.monotonic()-started
            pkg.require(elapsed < 8, 'bounded source refusal')
            receipt = dict(observed, label=label, status=response.status, headers=headers,
                           seconds=elapsed, source_allowed=int(row['expected'] != 403))
            pkg.save(s.OUT/(label+'-http.json'), receipt)
            self.probes.append(receipt)
        finally:
            connection.close()

    def source_checks(self):
        before = self.idle('before-sources')
        for row in self.plan['matrix']:
            self.probe(row)
        after = self.idle('after-sources')
        for name in ('q4t_requests_total', 'q4t_requests_success_total',
                     'q4t_structured_workers_started_total', 'q4t_structured_workers_reaped_total'):
            pkg.require(before[name] == after[name] == 0, 'rejected source reached model: '+name)
        pkg.save(s.OUT/'sources.json', dict(probes=self.probes, before=before, after=after))

    def logs(self):
        rows = [json.loads(line) for line in (s.OUT/'nginx/stdout.log').read_bytes().splitlines()]
        pkg.require(len(rows) == 25, 'expected TLS request count')
        by_id = {v['gateway_id']: v for v in rows}
        pkg.require(len(by_id) == len(rows), 'duplicate gateway request ids')
        for row in self.probes:
            observed = by_id[row['headers']['x-q4t-gateway-request-id']]
            pkg.require(observed['status'] == row['status'] and
                observed['source_allowed'] == row['source_allowed'], 'source access record differs')
            if row['status'] in (401, 403, 404):
                pkg.require(observed['upstream_status'] in ('', '-'), 'refused request proxied')
        pkg.require(sum(v['source_allowed'] == 0 for v in rows) == 16, 'refusal accounting')
        for row in rows:
            pkg.require(set(row) == {'gateway_id', 'client', 'lane', 'status', 'bytes', 'seconds',
                'upstream_status', 'connection_limit', 'rate_limit', 'source_allowed'}, 'access log fields')
        pkg.save(s.OUT/'gateway-rows.json', rows)

    def execute(self):
        result = dict(status='FAILED', counts=self.counts, long_run=False, performance_acceptance=False)
        def interrupted(signum, _):
            raise RuntimeError('short stage interrupted: '+str(signum))
        handlers = {sig: signal.signal(sig, interrupted) for sig in
                    (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)}
        signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-45)
        try:
            s.a.require_idle(s.OUT/'initial-gpu.json')
            for port in (PORT, TLS_PORT):
                with socket.socket() as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(('127.0.0.1', port))
            self.launch(True)
            self.source_checks()
            for index, client in enumerate(('alpha', 'beta')):
                self.current_client = index
                self.tools('project-'+client)
            metrics = self.idle('final-idle')
            pkg.save(s.OUT/'final-metrics.json', metrics)
            pkg.require(self.successes == metrics['q4t_requests_success_total'] == 4 and
                metrics['q4t_structured_workers_started_total'] ==
                metrics['q4t_structured_workers_reaped_total'] == 4, 'business/grammar accounting')
            self.stop(True)
            self.children.reap(s.OUT)
            self.logs()
            s.a.require_idle(s.OUT/'final-gpu.json')
            check()
            result.update(status='PASS_BOUNDED_GATEWAY_SOURCES', models=1, gateways=1,
                source_refusals=16, tls_probes=21, complete_responses=4, real_tools=2)
        except BaseException as error:
            result.update(status='ENV_ABORT' if 'ENV_ABORT' in str(error) else 'FAILED', error=repr(error))
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            for connection in self.sockets:
                connection.close()
            try:
                self.children.cleanup()
                result['cleanup_confirmed'] = True
            except BaseException as error:
                result.update(status='FAILED', cleanup_error=repr(error))
                raise
            finally:
                result['seconds'] = time.monotonic()-self.started
                pkg.save(s.OUT/'result.json', result)
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'run'))
    args = parser.parse_args()
    if args.action == 'prepare':
        print(json.dumps(prepare()))
    elif args.action == 'check':
        check()
        print('frozen source policy stage checked; no HTTP/model started')
    else:
        h.require_guard(OUT)
        Run(check()).execute()
