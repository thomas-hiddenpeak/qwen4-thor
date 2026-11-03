"""Same-process dynamic memory recovery then 64 fixed mixed business cycles."""
import http.client
import io
import json
import os
import socket
import subprocess
import time

from tools.serve_pressure import stage
from tools.serve_pressure import pressure
from tools.serve_pressure.observe import Monitor, plateau, sample
from tools.serve_admission import verify as base
from tools.serve_memory.verify import memory_contract, rejection_key
from isolated_scope import IsolatedScope
from chat_client import RemoteError, make_request, read_json, read_sse
from jsonschema import Draft202012Validator

h, ROOT, require = stage.h, stage.ROOT, stage.h.require


class FullWireClient:
    """Retain EOF including error DONE, before the strict client raises."""
    def __init__(self, port, folder, label):
        self.port, self.folder, self.label = port, folder, label

    def complete(self, body, *, sink):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=190)
        raw = bytearray()
        try:
            conn.request('POST', '/v1/chat/completions', json.dumps(body),
                         {'Content-Type': 'application/json'})
            response = conn.getresponse()
            headers = dict(response.getheaders())
            h.save(self.folder / (self.label+'-headers.json'),
                   dict(status=response.status, headers=headers))
            while True:
                part = response.readline(65537)
                if not part:
                    break
                raw.extend(part)
                sink(part)
                require(len(part) <= 65536 and len(raw) <= 8*1024**2, 'wire size bound')
            if response.status != 200:
                raise RemoteError(response.status, bytes(raw))
            expected = 'text/event-stream' if body['stream'] else 'application/json'
            require(response.headers.get_content_type() == expected, 'content type')
            result = (read_sse if body['stream'] else read_json)(io.BytesIO(raw))
            require(result.id == body['request_id'] and result.model == body['model'],
                    'response identity')
            return result
        finally:
            conn.close()


def content_bytes(path):
    if not path.exists():
        return 0
    count = 0
    for line in path.read_bytes().splitlines(keepends=True):
        if not line.endswith(b'\n') or not line.startswith(b'data: {'):
            continue
        value = json.loads(line[6:])
        for choice in value.get('choices', []):
            count += len(choice.get('delta', {}).get('content', '').encode())
    return count


class Run(base.Run):
    def __init__(self, plan, output):
        super().__init__(plan, output)
        self.deadline = plan['started'] + stage.LIMITS['seconds'] - 60
        self.monitor = self.pressure = None
        self.preflights = 0
        self.quiescent = []

    def budget(self, kind=None):
        require(time.time() < self.deadline, 'original pressure-stage deadline')
        if self.monitor:
            self.monitor.check()
        if kind:
            require(self.counts[kind] < stage.LIMITS[kind], 'stage budget: ' + kind)
            self.counts[kind] += 1
            h.save(self.out / f'attempt-{kind}-{self.counts[kind]:03d}.json',
                   dict(kind=kind, counts=self.counts.copy(), time=time.time()))

    def new_folder(self, name):
        self.folder = self.out / name
        self.folder.mkdir()

    def preflight(self, label):
        self.preflights += 1
        body = dict(request=base.ordinary(), include_token_ids=True)
        self.schemas['preflight'].validate(body)
        h.save(self.folder / (label+'-request.json'), body)
        value = self.http('POST', '/v1/chat/preflight', body)
        h.save(self.folder / (label+'-response.json'), value)
        require(value['status'] == 200, 'preflight failed')
        Draft202012Validator(h.read(ROOT / 'docs/serve-preflight-v1.schema.json')).validate(
            json.loads(value['raw']))

    def header_refusal(self, label, path):
        self.budget('negatives')
        request = (f'POST {path} HTTP/1.1\r\nHost: localhost\r\n'
                   f'X-Request-Id: pressure.{label}\r\nContent-Type: application/json\r\n'
                   'Content-Length: 16777216\r\n\r\n').encode()
        (self.folder / (label+'-request.bin')).write_bytes(request)
        raw = bytearray()
        started = time.monotonic()
        try:
            with socket.create_connection(('127.0.0.1', self.port), timeout=5) as sock:
                sock.sendall(request)
                while part := sock.recv(65536):
                    raw.extend(part)
                    require(len(raw) <= 65536, 'refusal wire too large')
        finally:
            path = self.folder / (label+'-wire.bin')
            path.write_bytes(raw)
            h.save(self.folder / (label+'-elapsed.json'), dict(seconds=time.monotonic()-started))
        header, body = bytes(raw).split(b'\r\n\r\n', 1)
        value = json.loads(body)
        self.transport.validate(value)
        require(header.startswith(b'HTTP/1.1 503 ') and
                value['error']['code'] == 'memory_pressure', 'header pressure refusal')
        self.wires.append(dict(file=h.identity(path), envelopes=1, success=False))

    def pressure_group(self):
        self.new_folder('pressure')
        initial = self.drain('before')
        memory_contract(initial, opened=True, floor=stage.FLOOR)
        # Freeze a safe operating window; do not retune the floor after launch.
        available = pressure.memory()['MemAvailable']
        require((stage.FLOOR+1024)*pressure.MIB <= available <=
                (stage.FLOOR+7168)*pressure.MIB, 'outside frozen pressure window')
        holder_body = make_request([dict(role='user', content=
            '从1开始逐行写出整数直到10000，每行一个，不要省略，不要解释，持续输出。')], [],
            stream=True, max_tokens=1024, timeout_ms=180000)
        holder = self.submit('holder', holder_body,
                            client=FullWireClient(self.port, self.folder, 'holder'))
        wire = self.folder / 'holder-wire.bin'
        self.wait('holder-running', lambda m: m['q4t_seq_slots_free'] == 0 and
                  content_bytes(wire) > 0)
        queued = self.submit('queued-strict', base.constrained(stream=True),
                            client=FullWireClient(self.port, self.folder, 'queued-strict'))
        self.wait('strict-queued', lambda m: m['q4t_queued_requests'] == 1 and
                  m['q4t_structured_workers_active'] == 1 and m['q4t_seq_slots_free'] == 0)
        self.pressure = pressure.Pressure(self.folder)
        try:
            row = self.pressure.command('status')
            while row['after']['MemAvailable'] > (stage.FLOOR-512)*pressure.MIB:
                self.budget()
                require(holder.thread.is_alive(), 'holder completed before pressure')
                row = self.pressure.command('add')
            blocked = self.wait('memory-closed', lambda m: m['q4t_memory_admission_open'] == 0)
            memory_contract(blocked, opened=False, floor=stage.FLOOR)
            queued.thread.join(timeout=5)
            require(not queued.thread.is_alive() and isinstance(queued.error, RemoteError) and
                    queued.error.code == 'memory_pressure', 'queued strict did not reject')
            headers = h.read(self.folder / 'queued-strict-headers.json')
            response = dict(**headers, raw=(self.folder / 'queued-strict-wire.bin').read_text())
            base.error_contract(response, 200, 'memory_pressure', stream=True)
            self.check_wire(queued, success=False)
            self.wait('queued-reaped', lambda m: m['q4t_queued_requests'] == 0 and
                m['q4t_structured_workers_active'] == 0 and m['q4t_structured_active'] == 0 and
                m['q4t_seq_slots_free'] == 0)
            before = content_bytes(wire)
            self.wait('holder-continues', lambda m: m['q4t_memory_admission_open'] == 0 and
                      content_bytes(wire) > before and holder.thread.is_alive(), timeout=3)
            h.save(self.folder / 'active-progress.json', dict(before=before, after=content_bytes(wire)))
            for label, path in (('chat-1', '/v1/chat/completions'),
                                ('chat-2', '/v1/chat/completions'),
                                ('preflight', '/v1/chat/preflight')):
                self.pressure.command('status')
                self.header_refusal(label, path)
            for path, status in (('/readyz', 503), ('/livez', 200), ('/healthz', 200)):
                value = self.http('GET', path)
                h.save(self.folder / ('blocked'+path.replace('/', '-')+'.json'), value)
                require(value['status'] == status, 'control under memory pressure')
                if path != '/healthz':
                    require(json.loads(value['raw']) ==
                        dict(status='memory_pressure', live=True, ready=False), 'readiness state')
            self.budget('negatives')
            unknown = self.http('POST', '/v1/requests/cancel',
                dict(request_id='pressure-unknown', cancel_token='a'*64))
            h.save(self.folder / 'unknown-cancel.json', unknown)
            require(unknown['status'] == 404 and
                json.loads(unknown['raw'])['error']['code'] == 'request_not_found', 'cancel isolation')
            self.pressure.command('status')
            self.cancel(holder, 'holder')
            raw = wire.read_bytes()
            require(raw.count(b'data: [DONE]') == 1 and raw.rstrip().endswith(b'data: [DONE]'),
                    'cancel terminal missing/duplicated')
            drained = self.drain('closed-drained')
            require(drained['q4t_memory_admission_open'] == 0 and
                    drained[rejection_key('headers', 'memory_pressure')] == 3 and
                    drained[rejection_key('execute', 'memory_pressure')] == 1 and
                    drained[rejection_key('prepare', 'memory_pressure')] == 0,
                    'rejection accounting or premature recovery')
            row = self.pressure.command('status')
            while row['after']['MemAvailable'] < (stage.FLOOR+128)*pressure.MIB:
                self.budget()
                row = self.pressure.command('remove')
            band = self.wait('hysteresis-band', lambda m:
                m['q4t_memory_available_bytes'] >= stage.FLOOR*pressure.MIB, timeout=2)
            require(band['q4t_memory_available_bytes'] < (stage.FLOOR+512)*pressure.MIB and
                    band['q4t_memory_admission_open'] == 0 and band['q4t_ready'] == 0,
                    'hysteresis not observed inside recovery band')
            time.sleep(.3)
            ready = self.http('GET', '/readyz')
            h.save(self.folder / 'hysteresis-ready.json', ready)
            require(ready['status'] == 503, 'recovery opened inside hysteresis band')
            row = self.pressure.command('release')
            require(row['allocated'] == 0, 'pressure retained after release')
        finally:
            self.pressure.close()
            h.save(self.folder / 'pressure-exit.json', dict(returncode=self.pressure.process.returncode))
        recovered = self.wait('same-process-recovered', lambda m: m['q4t_memory_admission_open'] == 1)
        memory_contract(recovered, opened=True, floor=stage.FLOOR)
        self.health('recovered')
        h.save(self.folder / 'same-process.json', sample(self.server, self.expected_start))
        self.preflight('recovery-preflight')
        self.normal(self.submit('recovery-ordinary', base.ordinary()))
        self.strict(self.submit('recovery-strict', base.constrained()))
        self.project_tool()

    def strict(self, pending):
        result = pending.finish(timeout=10)
        self.check_wire(pending)
        require(result.finish_reason == 'stop' and json.loads(result.message['content']) ==
                dict(ok=True), 'strict task quality')

    def mixed_group(self):
        for cycle in range(1, stage.CYCLES+1):
            self.new_folder(f'cycle-{cycle:02d}')
            self.preflight('preflight')
            # Three real concurrent clients; no synthetic output/pause injection.
            plain = self.submit('ordinary-json', base.ordinary())
            stream = self.submit('ordinary-sse', base.ordinary(stream=True))
            strict = self.submit('strict', base.constrained(stream=bool(cycle % 2)))
            self.normal(plain)
            self.normal(stream)
            self.strict(strict)
            self.project_tool()
            self.drain('cycle-drained')
            self.health('cycle')
            # Allow the just-completed control handler to exit before endpoints.
            time.sleep(.05)
            row = sample(self.server, self.expected_start)
            self.quiescent.append(row)
            h.save(self.folder / 'quiescent.json', row)
            print(f'mixed cycle {cycle}/{stage.CYCLES} passed', flush=True)
        h.save(self.out / 'resource-envelope.json', plateau(self.quiescent))

    def model(self):
        self.budget('starts')
        self.new_folder('model')
        folder = self.folder
        argv = [self.plan['binary']['path'], 'serve', '--model-dir', self.plan['model_dir'],
            '--host', '127.0.0.1', '--port', str(self.port), '--max-prefill', '2048',
            '--max-len', '8192', '--max-seq', '1', '--max-inflight-requests', '8',
            '--max-queued-requests', '2', '--min-available-memory-mib', str(stage.FLOOR)]
        env = {k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        self.server = IsolatedScope(argv, cwd=ROOT, env=env, log_path=folder/'server.log',
            unit=f'q4t-serve-pressure-20261103-{os.getpid()}.scope', memory_max=None,
            evidence=folder/'scope')
        try:
            self.expected_start = h.read(folder/'scope/identity.json')['start_ticks']
            self.monitor = Monitor(self.server, self.expected_start, folder)
            end = time.monotonic()+240
            while 'serving on port' not in (folder/'server.log').read_text():
                self.budget()
                require(self.server.poll() is None and time.monotonic() < end, 'startup failed')
                time.sleep(.25)
            config = self.http('GET', '/v1/config')
            h.save(folder/'configuration.json', config)
            value = json.loads(config['raw'])
            self.config_schema.validate(value)
            require(config['status'] == 200 and value['protocol_source_sha256'] ==
                self.plan['protocol_sha256'] and value['runtime']['mtp_enabled'] and
                value['memory_admission']['min_available_mib']['effective'] == stage.FLOOR,
                'configuration identity')
            self.pressure_group()
            self.mixed_group()
            self.new_folder('final')
            rows = self.drain('final')
            memory_contract(rows, opened=True, floor=stage.FLOOR)
            require(rows['q4t_requests_success_total'] == 324 and
                rows['q4t_requests_aborted_total'] == 1 and rows['q4t_requests_error_total'] == 1,
                'mixed final outcome accounting')
            self.health('final')
            self.server.snapshot('business-complete')
        finally:
            try:
                if self.pressure:
                    self.pressure.close()
                if self.monitor:
                    self.monitor.close()
            finally:
                try:
                    if self.server.poll() is None:
                        self.server.terminate()
                        try:
                            self.server.wait(timeout=45)
                        except subprocess.TimeoutExpired:
                            self.server.kill()
                            self.server.wait(timeout=10)
                    self.server.snapshot('payload-exited')
                finally:
                    self.server.close()
            for call in self.calls:
                call.thread.join(timeout=5)
            log = (folder/'server.log').read_text()
            clean = self.server.returncode == 0 and 'shutdown complete (0 in-flight remaining)' in log
            h.save(folder/'shutdown.json', dict(clean=clean, returncode=self.server.returncode,
                scope_removed=self.server.closed, clients_joined=all(not c.thread.is_alive() for c in self.calls)))
            require(clean and self.server.closed and all(not c.thread.is_alive() for c in self.calls),
                    'owned shutdown failed')
            stage.a.require_idle_gpu(folder/'gpu-after.json')

    def run(self):
        result = dict(status='FAILED')
        try:
            self.model()
            require(self.counts == stage.PLANNED and self.preflights == 65, 'frozen case count')
            h.save(self.out/'wire-schema-result.json', dict(passed=True, records=self.wires))
            result.update(status='PASS_BOUNDED_PRESSURE_AND_MIXED',
                same_process_model_recovery_tested=True, fixed_mixed_cycles=stage.CYCLES,
                physical_ram_hard_limit_proved=False, long_duration_soak_proved=False,
                deployment_changed=False, automatic_retries=0)
        except BaseException as error:
            result['error'] = repr(error)
            raise
        finally:
            result.update(counts=self.counts, preflights=self.preflights,
                          elapsed_seconds=time.monotonic()-self.started)
            h.save(self.out/'result.json', result)
