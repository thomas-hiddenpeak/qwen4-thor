"""Frozen direct-runner/TLS client isolation short acceptance; never installs."""
import argparse
import copy
from dataclasses import asdict
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_clients import cpu
from tools.serve_gateway import gateway as g, client_policy as cp
from tools.serve_gateway.acceptance import cancelled_wire
from tools.serve_gateway.test_gateway import certificate, example, VENDOR
from tools.serve_gateway.transport_tests import TLSConnection
from tools.serve_cuda_quota_integration.verify import Children
from tools.serve_service import acceptance as a, package as pkg, supervisor as sup
from tools.serve_backpressure.verify import metrics as parse_metrics
from jsonschema import Draft202012Validator

BASE = cpu.BASE
PLAN = BASE/'short-plan.json'
OUT = BASE/'short'
LIMITS = dict(starts=2, generation_attempts=22, controls=120, seconds=600)
PORT, TLS_PORT = 18127, 18447


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def verify_sources(values):
    for name, digest in values.items():
        require(cpu.sha(ROOT/name) == digest, 'source changed: '+name)


def prepared_identity(plan):
    require(plan['limits'] == LIMITS and plan['port'] == PORT and
            plan['tls_port'] == TLS_PORT, 'frozen short scope changed')
    verify_sources(plan['sources'])
    for name, digest in plan['default'].items():
        require(cpu.sha(ROOT/name) == digest, 'default pair changed')
    for name, digest in plan['binaries'].items():
        require(cpu.sha(ROOT/name) == digest, 'candidate binary changed')
    for path, digest in plan['local'].items():
        require(cpu.sha(path) == digest, 'local frozen input changed')
    for name, digest in plan['model_inputs'].items():
        require(cpu.sha(Path(plan['model_dir'])/name) == digest, 'model identity changed')
    for path in plan['day_plans']:
        require(not (Path(path).parent/'execution').exists(), 'long run was started')
    return plan


def prepare():
    require(not PLAN.exists() and not OUT.exists(), 'preserve earlier short evidence')
    built = pkg.read(BASE/'build-v2/result.json')
    tested = pkg.read(BASE/'cpu-v1/result.json')
    require(built['passed'] and tested['passed'], 'host evidence missing')
    verify_sources(pkg.read(BASE/'build-v2/sources.json'))
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed driver before freeze')
    private = BASE/'short-private'
    private.mkdir(mode=0o700)
    cert, key = certificate(private)
    front = example(TLS_PORT, PORT)
    front.update(per_client_inflight=4, global_inflight=8, per_client_rps=100, burst=128)
    policy = dict(version=1, gateway_token=secrets.token_hex(32), clients=[
        dict(id='alpha', max_inflight=1, max_control_inflight=2,
             max_prompt_tokens=12000, max_output_tokens=512, max_body_bytes=131072),
        dict(id='beta', max_inflight=1, max_control_inflight=2,
             max_prompt_tokens=9000, max_output_tokens=64, max_body_bytes=65536)])
    g.save(private/'clients.json', json.dumps(policy).encode())
    g.save(private/'gateway.json', json.dumps(front).encode())
    gateway = g.prepare(private/'gateway.json', pkg.read(VENDOR)['binary'], cert, key,
                        private/'gateway', private/'clients.json')
    previous = pkg.read(ROOT/'.q4t-work/serve-cuda-quota-integration-20261104/package-v2/plan.json')
    paths = subprocess.check_output(['git', 'ls-files', 'tools', 'cmake'],
                                    cwd=ROOT, text=True).splitlines()
    paths += [str(p.relative_to(ROOT)) for p in (ROOT/'docs').glob('serve-*.json')]
    paths += ['docs/MODEL.md', 'docs/SERVE_CLIENT_SHORT_2026-11-04.md']
    values = dict(pkg.read(BASE/'build-v2/sources.json'))
    values.update({name: cpu.sha(ROOT/name) for name in paths})
    days = [ROOT/'.q4t-work/serve-soak-20261103/day/plan.json',
            ROOT/'.q4t-work/serve-cuda-quota-integration-20261104/operator-soak/day/plan.json']
    protocol = pkg.read(cpu.ENGINE/'generated/q4t/server/chat_protocol_sources.json')
    protocol_hash = hashlib.sha256(''.join(f'{n}:{protocol[n]}\n' for n in sorted(protocol)).encode()).hexdigest()
    local = {str(p): cpu.sha(p) for p in [private/'clients.json', private/'gateway.json',
                BASE/'cpu-v1/result.json', BASE/'build-v2/result.json', *days]}
    value = dict(limits=LIMITS, port=PORT, tls_port=TLS_PORT, sources=values,
        default={name: cpu.sha(ROOT/name) for name in ('build/q4t', 'build/q4t.release.json')},
        binaries=built['binaries'], binary=str(cpu.ENGINE/'q4t'),
        lifecycle=str(cpu.ENGINE/'q4t_http_lifecycle_tests'), gateway=gateway,
        local=local, policy=str(private/'clients.json'), frontend=str(private/'gateway.json'),
        protocol=protocol_hash, model_dir=previous['model_dir'], model_inputs=previous['model_inputs'],
        day_plans=list(map(str, days)), source_commit=subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        scope='Direct runner + real TLS; not managed package deployment or long stability',
        default_installation=False, performance_acceptance=False)
    cpu.save(PLAN, value)
    prepared_identity(value)
    return dict(prepared=True, starts=0, requests=0, limits=LIMITS)


class Run:
    tools = a.Run.tools

    def __init__(self, plan):
        self.plan = plan
        self.port = PORT
        self.started = time.monotonic()
        self.counts = dict(starts=0, generation_attempts=0, controls=0)
        self.children = Children(OUT/'lease')
        self.front = pkg.read(plan['frontend'])
        self.policy = cp.read(plan['policy'])[1]
        self.tls = False
        self.context = ssl.create_default_context(cafile=Path(plan['gateway']['package'])/'certificate.pem')
        self.sockets = []
        self.model = self.nginx = None
        self.successes = 0
        self.cancelled = 0
        a.OUT = OUT

    def budget(self, kind):
        require(time.monotonic()-self.started < LIMITS['seconds'], 'short wall budget')
        require(self.counts[kind] < LIMITS[kind], 'short budget: '+kind)
        self.counts[kind] += 1
        cpu.save(OUT/f'attempt-{kind}-{self.counts[kind]:03d}.json', dict(self.counts))

    def begin(self, label, body=None, *, path='/v1/chat/completions', method='POST',
              client=0, headers=None, direct=False):
        self.budget('generation_attempts' if path == '/v1/chat/completions' else 'controls')
        secure = self.tls and not direct
        connection = (TLSConnection(self.front['server_name'], TLS_PORT,
                                    context=self.context, timeout=90) if secure else
                      http.client.HTTPConnection('127.0.0.1', PORT, timeout=90))
        self.sockets.append(connection)
        fields = {'Content-Type': 'application/json'}
        if secure and client is not None:
            fields['Authorization'] = 'Bearer '+self.front['clients'][client]['api_key']
        if body and body.get('request_id'):
            fields['X-Request-Id'] = body['request_id']
        if headers:
            fields.update(headers)
        cpu.save(OUT/(label+'-request.json'), dict(path=path, method=method, body=body,
                                                  client=client, direct=direct))
        connection.request(method, path, json.dumps(body) if body is not None else None, fields)
        response = connection.getresponse()
        cpu.save(OUT/(label+'-http.json'), dict(status=response.status, headers=dict(response.getheaders())))
        return connection, response

    def record(self, label, response, raw, expected):
        require(len(raw) <= 8*1024**2, 'response bound')
        g.save(OUT/(label+'-wire.bin'), raw)
        require(response.status == expected, 'HTTP status: '+label)
        return raw

    def request(self, label, body=None, expected=200, **kwargs):
        connection, response = self.begin(label, body, **kwargs)
        try:
            raw = self.record(label, response, response.read(8*1024**2+1), expected)
            value = json.loads(raw)
            if expected >= 400:
                schema = pkg.read(ROOT/'docs/serve-transport-v1.schema.json')
                Draft202012Validator(schema).validate(value)
            return value
        finally:
            connection.close()

    def complete(self, label, body, client=0):
        connection, response = self.begin(label, body, client=client)
        started = time.monotonic()
        try:
            raw = self.record(label, response, response.read(8*1024**2+1), 200)
            result = (a.read_sse if body.get('stream') else a.read_json)(io.BytesIO(raw))
            require(result.id == body['request_id'] and result.model == body['model'],
                    'completion identity: '+label)
            self.successes += 1
            cpu.save(OUT/(label+'-response.json'), dict(asdict(result), elapsed=time.monotonic()-started))
            return result
        finally:
            connection.close()

    def get_metrics(self, label):
        connection, response = self.begin(label, path='/metrics', method='GET', direct=True)
        try:
            raw = self.record(label, response, response.read(1024**2), 200)
            text = raw.decode()
            return parse_metrics(text), text
        finally:
            connection.close()

    def idle(self, label):
        until = time.monotonic()+5
        while True:
            values, raw = self.get_metrics(label+'-'+str(self.counts['controls']))
            active = [float(line.rsplit(' ', 1)[1]) for line in raw.splitlines()
                      if line.startswith('q4t_client_requests_active{')]
            if all(values[name] == 0 for name in a.EMPTY) and not any(active):
                require(values['q4t_gpu_healthy'] == 1 and values['q4t_seq_slots_free'] == 1,
                        'idle model unhealthy')
                require(values['q4t_structured_workers_started_total'] ==
                        values['q4t_structured_workers_reaped_total'], 'grammar ownership retained')
                return values
            require(time.monotonic() < until, 'request leases retained')
            time.sleep(.1)

    def launch(self, scoped):
        self.budget('starts')
        label = 'scoped-model' if scoped else 'legacy-model'
        extra = []
        if scoped:
            for name in ('sessions', 'prefixes'):
                (OUT/name).mkdir(mode=0o700)
            extra = ['--client-policy-file', self.plan['policy'], '--max-len', '16384',
                     '--seq-capacities', '16384', '--session-cache-dir', str(OUT/'sessions'),
                     '--prefix-cache-dir', str(OUT/'prefixes'), '--prefix-cache-checkpoints', '8192']
        command = [self.plan['binary'], 'serve', '--model-dir', self.plan['model_dir'],
                   '--host', '127.0.0.1', '--port', str(PORT), '--max-seq', '1',
                   '--max-prefill', '2048', '--max-len', '8192', '--max-inflight-requests', '8',
                   '--max-queued-requests', '2', *extra]
        env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and
               k not in ('LD_PRELOAD', 'LD_LIBRARY_PATH')}
        self.model = self.children.launch(OUT/label, command, env)
        until = time.monotonic()+180
        while 'serving on port' not in (OUT/label/'stderr.log').read_text():
            require(self.children.running(self.model) and time.monotonic() < until, 'model startup failed')
            time.sleep(.1)
        config = self.request(label+'-config', path='/v1/config', method='GET', direct=True)
        Draft202012Validator(pkg.read(ROOT/'docs/serve-config-v1.schema.json')).validate(config)
        require(config['protocol_source_sha256'] == self.plan['protocol'], 'runtime protocol drift')
        cp.observed_matches(config['client_policy'], cp.binding(Path(self.plan['policy']).read_bytes()) if scoped else None)
        if scoped:
            package = Path(self.plan['gateway']['package'])
            manifest = g.check(package, self.plan['gateway']['manifest_sha256'])
            self.nginx = self.children.launch(OUT/'nginx',
                [manifest['nginx']['path'], '-e', 'stderr', '-p', str(package)+'/',
                 '-c', str(package/'nginx.conf')], env)
            until = time.monotonic()+10
            while True:
                require(self.children.running(self.nginx) and time.monotonic() < until, 'nginx startup failed')
                try:
                    with socket.create_connection(('127.0.0.1', TLS_PORT), .1):
                        break
                except ConnectionRefusedError:
                    time.sleep(.05)
            self.tls = True

    def stop(self, scoped):
        if scoped:
            self.children.signal(self.nginx, signal.SIGQUIT)
            self.children.wait(self.nginx, OUT/'nginx', 40)
            self.nginx = None
        self.children.signal(self.model, signal.SIGTERM)
        folder = OUT/('scoped-model' if scoped else 'legacy-model')
        self.children.wait(self.model, folder, 45)
        require('shutdown complete (0 in-flight remaining)' in (folder/'stderr.log').read_text(),
                'model did not drain')
        self.model = None

    def limits(self):
        error = self.request('missing-gateway', a.ordinary(), direct=True, expected=401)
        require(error['error']['code'] == 'client_authentication_failed', 'unauthenticated backend accepted')
        body = a.ordinary()
        body['max_tokens'] = 65
        for label, path, wrapped in [
                ('output-chat', '/v1/chat/completions', body),
                ('output-preflight', '/v1/chat/preflight', dict(request=body))]:
            error = self.request(label, wrapped, path=path, client=1, expected=400)
            require(error['error']['code'] == 'client_output_limit_exceeded', 'output budget drift')
        body = a.ordinary()
        body.pop('max_tokens')
        result = self.request('beta-default', dict(request=body), path='/v1/chat/preflight', client=1)
        require(result['output']['requested_tokens'] == 64, 'default exceeds client output budget')
        forged = self.request('forged-client', dict(request=a.ordinary()), path='/v1/chat/preflight',
            headers={'X-Q4T-Client-Id': 'beta', 'X-Q4T-Gateway-Token': '0'*64})
        require(forged['client_limits']['max_output_tokens'] == 512, 'untrusted identity reached runner')
        body = a.ordinary(True)
        body['messages'][0]['content'] = ' x'*10000
        body['max_tokens'] = 32
        for label, path, wrapped in [
                ('prompt-chat', '/v1/chat/completions', body),
                ('prompt-preflight', '/v1/chat/preflight', dict(request=body))]:
            error = self.request(label, wrapped, path=path, client=1, expected=400)
            require(error['error']['code'] == 'client_prompt_limit_exceeded', 'prompt budget drift')

    def cancellation(self):
        first, second = a.ordinary(True), a.ordinary(True)
        first['messages'][0]['content'] = '逐行列出从1到1000的数字，不要省略。'
        second['messages'] = copy.deepcopy(first['messages'])
        first['max_tokens'], second['max_tokens'] = 512, 64
        second['request_id'] = first['request_id']
        ac, ar = self.begin('cancel-alpha', first)
        aprefix = ar.readline()
        require(ar.status == 200 and aprefix.startswith(b'data: '), 'alpha stream not started')
        bc, br = self.begin('same-id-beta', second, client=1)
        bprefix = br.readline()
        require(br.status == 200 and bprefix.startswith(b'data: '), 'same ID rejected across clients')
        body = dict(request_id=first['request_id'], cancel_token=first['cancel_token'])
        error = self.request('cross-client-cancel', body, path='/v1/requests/cancel', client=1, expected=404)
        require(error['error']['code'] == 'request_not_found', 'cross-client cancellation accepted')
        error = self.request('alpha-quota', a.ordinary(), expected=429)
        require(error['error']['code'] == 'client_capacity_exhausted', 'runner client quota not active')
        self.request('alpha-control', path='/v1/models', method='GET')
        self.request('own-cancel', body, path='/v1/requests/cancel', expected=202)
        araw = self.record('cancel-alpha', ar, aprefix+ar.read(8*1024**2), 200)
        cpu.save(OUT/'cancel-terminal.json', cancelled_wire(araw, first['request_id']))
        self.cancelled += 1
        braw = self.record('same-id-beta', br, bprefix+br.read(8*1024**2), 200)
        value = a.read_sse(io.BytesIO(braw))
        require(value.id == second['request_id'] and value.finish_reason in ('stop', 'length'), 'beta cancelled by alpha')
        cpu.save(OUT/'same-id-beta-response.json', asdict(value))
        self.successes += 1
        ac.close(); bc.close()
        self.idle('cancel-recovery')

    def caches(self):
        # Fixed long raw template; preflight checks its actual tokenizer length.
        prompt = '<|im_start|>system\n'+' x'*8200+'<|im_end|>\n'+\
                 '<|im_start|>user\n逐行列出1到1000，不要省略。<|im_end|>\n'+\
                 '<|im_start|>assistant\n<think>\n\n</think>\n\n'
        body = a.ordinary(True)
        for name in ('messages', 'tools', 'parse_response', 'enable_thinking',
                     'chat_template_kwargs', 'include_reasoning', 'tool_choice',
                     'parallel_tool_calls'):
            body.pop(name, None)
        session, scope = secrets.token_hex(32), secrets.token_hex(32)
        body.update(prompt=prompt, max_tokens=8, session_id=session, prefix_cache_scope=scope)
        planned = self.request('cache-preflight', dict(request=body), path='/v1/chat/preflight')
        require(8192 < planned['prompt_tokens'] < 9000, 'fixed cache prompt outside frozen limits')
        first = self.complete('cache-alpha-seed', body)
        require(first.usage['completion_tokens'] == 8, 'cache seed did not reach length boundary')
        branch = prompt+first.message['content']+'<|im_end|>\n<|im_start|>user\n'+\
                 '只回复数字43。<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
        def request(label, client, *, private=True):
            req = copy.deepcopy(body)
            req.update(prompt=branch, max_tokens=16, request_id='cache-'+label,
                       cancel_token=secrets.token_hex(32))
            if not private:
                req.pop('session_id')
            result = self.complete('cache-'+label, req, client)
            require(result.message['content'].strip() == '43', 'cache answer quality: '+label)
            return result.usage['prompt_tokens_details']['cached_tokens']
        beta = request('beta-isolated', 1)
        alpha = request('alpha-private', 0)
        ashared = request('alpha-shared', 0, private=False)
        bshared = request('beta-shared', 1, private=False)
        require(beta == 0 and alpha > 8192 and ashared == bshared == 8192,
                'cache namespace/priority mismatch')
        cpu.save(OUT/'cache-isolation.json', dict(beta_miss=beta, alpha_private=alpha,
                 alpha_shared=ashared, beta_shared=bshared, exact_answer='43'))

    def execute(self):
        OUT.mkdir(mode=0o700, exist_ok=True)
        lock = a.guard.common_lock()
        st = lock.stat()
        require((st.st_dev, st.st_ino) == (66305, 3294067), 'GPU lock replaced')
        lease = pkg.read(lock)
        require(lease['pid'] == os.getppid() and lease['state'] == 'held' and
                not a.lock_available(lock), 'requires full-phase GPU guard')
        result = dict(status='FAILED', limits=LIMITS, installed=False, long_run=False)
        def interrupted(*_):
            raise RuntimeError('short driver interrupted or wall budget expired')
        signals = {s: signal.signal(s, interrupted) for s in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)}
        signal.alarm(LIMITS['seconds'])
        try:
            a.require_idle(OUT/'initial-gpu.json')
            child = self.children.launch(OUT/'lifecycle', [self.plan['lifecycle']])
            self.children.wait(child, OUT/'lifecycle', 90)
            require('27 tests, 27 passed, 0 failed, 0 skipped' in
                    (OUT/'lifecycle/stdout.log').read_text(), 'native HTTP lifecycle gate')
            self.launch(False)
            value = self.complete('legacy-quality', a.ordinary())
            require(value.message['content'].strip() == '710003', 'legacy quality')
            self.idle('legacy-idle'); self.stop(False)
            self.launch(True)
            self.limits()
            self.cancellation()
            value = self.complete('scoped-quality', a.ordinary(True))
            require(value.message['content'].strip() == '710003', 'scoped recovery quality')
            value = self.complete('scoped-structured', a.strict_json())
            require(json.loads(value.message['content']) == {'ok': True}, 'structured quality')
            self.tools('project')
            self.caches()
            metrics = self.idle('scoped-idle')
            cpu.save(OUT/'final-metrics.json', metrics)
            self.stop(True)
            for folder in ('legacy-model', 'scoped-model', 'nginx'):
                for name in ('stdout.log', 'stderr.log'):
                    raw = (OUT/folder/name).read_bytes()
                    for secret in [self.policy['gateway_token'],
                                   *(c['api_key'] for c in self.front['clients'])]:
                        require(secret.encode() not in raw, 'credential leaked to log')
            self.children.reap(OUT)
            a.require_idle(OUT/'final-gpu.json')
            prepared_identity(self.plan)
            require(self.successes == 11 and self.cancelled == 1, 'generation accounting')
            result.update(status='PASS_BOUNDED_CLIENT_ISOLATION', native_tests=27,
                          successful_generations=self.successes, cancelled_generations=1,
                          real_tools=1, cleanup_confirmed=True)
        except BaseException as error:
            result['error'] = repr(error)
            raise
        finally:
            signal.alarm(0)
            for connection in self.sockets:
                connection.close()
            self.children.cleanup()
            for s, previous in signals.items():
                signal.signal(s, previous)
            result.update(counts=self.counts, seconds=time.monotonic()-self.started)
            cpu.save(OUT/'result.json', result)
        return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'run'))
    args = parser.parse_args()
    if args.action == 'prepare':
        value = prepare()
    else:
        plan = prepared_identity(pkg.read(PLAN))
        if args.action == 'run':
            require(not (OUT/'result.json').exists(), 'preserve prior execution')
            with sup.processes.child_subreaper():
                value = Run(plan).execute()
        else:
            value = dict(checked=True, started=False)
    print(json.dumps(value))


if __name__ == '__main__':
    main()
