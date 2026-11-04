"""One real model: partition saturation, control cancellation and tool recovery."""
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.parse import urlencode

from tools.serve_observation_capacity import stage
from tools.serve_ops import bundle, contracts, identity
from tools.serve_ops.verify import Run as OperationsRun
from tools.serve_admission import verify as base
from tools.serve_pressure.verify import FullWireClient, content_bytes
from chat_client import make_request

h, ROOT, require = stage.h, stage.ROOT, stage.h.require


def checked_error(raw, status, code, validator):
    header, body = raw.split(b'\r\n\r\n', 1)
    require(header.startswith(f'HTTP/1.1 {status} '.encode()), 'negative HTTP status')
    fields = dict(line.split(b':', 1) for line in header.split(b'\r\n')[1:])
    require(int(fields[b'Content-Length'].strip()) == len(body), 'incomplete error body')
    value = json.loads(body)
    validator.validate(value)
    require(value['error']['code'] == code, 'negative logical code')
    return value


def check_accounting(before, after):
    expected = dict(q4t_requests_total=4, q4t_requests_success_total=3,
        q4t_requests_error_total=0, q4t_requests_aborted_total=1,
        q4t_structured_workers_started_total=2,
        q4t_structured_workers_reaped_total=2)
    for name, count in expected.items():
        require(after[name]-before[name] == count, 'business accounting: '+name)
    for kind, count in (('unclassified', 0), ('business', 1), ('observation', 4), ('control', 0)):
        key = f'q4t_connection_rejections_total{{kind="{kind}"}}'
        require(after[key]-before[key] == count, 'partition accounting: '+kind)
    for reason, count in (('connection_capacity', 5), ('body_eof', 127), ('unexpected_body', 4)):
        key = f'q4t_http_receive_failures_total{{reason="{reason}"}}'
        require(after[key]-before[key] == count, 'receive accounting: '+reason)
    require(after['q4t_business_connections'] == after['q4t_control_connections'] == 0 and
            after['q4t_observation_connections'] == 1, 'scrape ownership')
    require(all(after[k] == 0 for k in base.EMPTY) and
            after['q4t_seq_slots_free'] == after['q4t_seq_slots_total'] == 1,
            'resources retained')


class Run(OperationsRun):
    def __init__(self, plan, output):
        super().__init__(plan, output)
        self.deadline = plan['started']+stage.LIMITS['seconds']-60
        self.control_count = 0
        self.held = []
        self.hold_started = self.prom_started = None

    def budget(self, kind=None):
        require(time.time() < self.deadline, 'frozen observation-stage deadline')
        if self.hold_started is not None:
            require(time.monotonic()-self.hold_started < 25, 'partial-body window exhausted')
        if self.prom_started is not None:
            require(time.monotonic()-self.prom_started < 30, 'temporary scrape reservation exhausted')
        if kind:
            require(self.counts[kind] < stage.LIMITS[kind], 'frozen attempts: '+kind)
            self.counts[kind] += 1
            h.save(self.out/f'attempt-{kind}-{self.counts[kind]:03d}.json',
                   dict(kind=kind, counts=self.counts.copy(), time=time.time()))

    def control(self, amount=1):
        self.budget()
        require(self.control_count+amount <= stage.LIMITS['observations'], 'control observation budget')
        self.control_count += amount

    def http(self, method, path, body=None, *, timeout=5):
        self.control()
        value = super().http(method, path, body, timeout=timeout)
        if value['status'] >= 400:
            self.budget('negatives')
        return value

    def wait(self, label, predicate, *, timeout=10):
        end, samples = time.monotonic()+timeout, []
        try:
            while True:
                rows = self.rows()
                samples.append(dict(monotonic=time.monotonic(), metrics=rows))
                if predicate(rows):
                    return rows
                require(self.server.poll() is None and time.monotonic() < end, 'wait: '+label)
                time.sleep(.1)
        finally:
            h.save(self.folder/(label+'-samples.json'), samples)

    def collect(self, label, previous=None):
        self.control(5)
        require(self.counts['negatives']+3 <= stage.LIMITS['negatives'], 'diagnostic refusal reserve')
        argv = ['/usr/bin/python3', '-B', str(ROOT/'tools/serve_ops/diagnose.py'),
            '--base-url', self.base, '--pid', str(self.server.pid),
            '--release', self.plan['sidecar']['path'], '--log', str(self.folder/'server.log'),
            '--output', str(self.diag_root/label)]
        if previous:
            argv += ['--previous', str(self.diag_root/previous)]
        with (self.out/(label+'-diagnose.stdout')).open('xb') as stdout, \
             (self.out/(label+'-diagnose.stderr')).open('xb') as stderr:
            process = subprocess.run(argv, cwd=ROOT, stdout=stdout, stderr=stderr, timeout=15)
        h.save(self.out/(label+'-diagnose-command.json'), dict(argv=argv, returncode=process.returncode))
        value = bundle.read(self.diag_root/label)
        for observation in value['probes'].values():
            if observation.get('http_status', 0) >= 400:
                self.budget('negatives')
        expected = 'UNKNOWN' if label == 'observation-full' else 'HEALTHY' if label == 'before' else 'WARNING'
        require(value['state'] == expected and
                process.returncode == {'HEALTHY':0, 'WARNING':1, 'UNKNOWN':3}[expected],
                'diagnostic state: '+label)
        if label != 'observation-full':
            require(value['identity']['state'] == 'verified' and value['observability_version'] == 4,
                    'candidate diagnosis identity/version')
        else:
            require(all(value['probes'][p]['http_status'] == 503 for p in
                        ('/metrics', '/v1/config', '/v1/capabilities')) and
                    all(value['probes'][p]['http_status'] == 200 for p in ('/livez', '/readyz')),
                    'diagnostic partition boundary')
        return value

    def start_prometheus(self):
        # At most 30 seconds at 1 Hz, with 35 target requests reserved up front.
        # Prometheus API queries do not hit q4t and are recorded separately.
        self.control(35)
        rules = h.read(ROOT/'deploy/serve-ops/q4t.rules.yml')
        for group in rules['groups']:
            group['interval'] = '1s'
        h.save(self.prom_root/'validation-rules.json', rules)
        config = {'global':dict(scrape_interval='1s', scrape_timeout='800ms', evaluation_interval='1s'),
            'rule_files':[str(self.prom_root/'validation-rules.json')],
            'scrape_configs':[dict(job_name='q4t', static_configs=[dict(targets=[f'127.0.0.1:{self.port}'])])],
            'alerting':dict(alertmanagers=[])}
        h.save(self.prom_root/'config.json', config)
        argv = [self.plan['prometheus']['path'], '--config.file='+str(self.prom_root/'config.json'),
            '--storage.tsdb.path='+str(self.prom_root/'tsdb'),
            '--web.listen-address=127.0.0.1:'+str(self.plan['prometheus_port']),
            '--storage.tsdb.retention.time=1h', '--storage.tsdb.retention.size=64MB']
        h.save(self.prom_root/'command.json', dict(argv=argv, target_request_reservation=35,
            notification_receivers=[], source_policy=h.identity(ROOT/'deploy/serve-ops/q4t.rules.yml'),
            validation_interval_seconds=1, production_rule_interval_seconds=15,
            purpose='Actual metric parsing and saturated scraping, not production alert timing proof'))
        self.prom_started = time.monotonic()
        with (self.prom_root/'server.log').open('x') as log:
            self.prom = subprocess.Popen(argv, cwd=ROOT, stdout=log, stderr=log)
            self.prom_fd = os.pidfd_open(self.prom.pid)
        end = time.monotonic()+10
        while 'Server is ready to receive web requests.' not in (self.prom_root/'server.log').read_text():
            self.budget()
            require(self.prom.poll() is None and time.monotonic() < end, 'Prometheus startup')
            time.sleep(.1)
        owned = set()
        for fd in Path(f'/proc/{self.prom.pid}/fd').iterdir():
            try:
                target = os.readlink(fd)
            except FileNotFoundError:
                continue
            if target.startswith('socket:['):
                owned.add(target[8:-1])
        selected = identity.listener(Path('/proc/net/tcp').read_text(),
                                     self.plan['prometheus_port'], owned)
        require(selected['listener_scope'] == 'loopback', 'Prometheus not isolated')
        h.save(self.prom_root/'listener.json', dict(pid=self.prom.pid, **selected))

    def saturated_scrape(self):
        samples, end = [], time.monotonic()+6
        try:
            while True:
                query = '(q4t_business_connections{job="q4t"} == 128) and on (job, instance) (up{job="q4t"} == 1)'
                value = self.prom_get('/api/v1/query?'+urlencode(dict(query=query)))
                samples.append(value)
                if len(value['data']['result']) == 1:
                    require(value['data']['result'][0]['value'][1] == '128', 'saturated scrape value')
                    rules = self.prom_get('/api/v1/rules')
                    items = [r for g in rules['data']['groups'] for r in g['rules']]
                    if len(items) == 19 and all(r['health'] == 'ok' for r in items):
                        h.save(self.prom_root/'saturated-rules.json', rules)
                        return
                require(time.monotonic() < end, 'no actual saturated scrape')
                time.sleep(.1)
        finally:
            h.save(self.prom_root/'saturated-samples.json', samples)

    def stop_prometheus(self):
        # Clean up even after the observation deadline, without invoking budget().
        try:
            super().stop_prometheus()
        finally:
            if self.prom_started is not None:
                elapsed = time.monotonic()-self.prom_started
                h.save(self.prom_root/'lifetime.json', dict(seconds=elapsed,
                    reserved_target_requests=35, exact_target_request_count=False))
                self.prom_started = None
                require(elapsed < 30, 'scrape lifetime exceeded reservation')

    def hold(self, label, method, path):
        self.budget()
        if self.hold_started is None:
            self.hold_started = time.monotonic()
        raw = (f'{method} {path} HTTP/1.1\r\nHost: localhost\r\n'
               f'X-Request-Id: observation.{label}\r\n'
               'Content-Type: application/json\r\nContent-Length: 1\r\n\r\n').encode()
        (self.folder/(label+'-request.bin')).write_bytes(raw)
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=3)
        self.held.append((label, sock, method))
        sock.sendall(raw)

    def release_held(self):
        # Trigger/read each expected rejection; keep full wire, not synthesized JSON.
        for label, sock, method in reversed(self.held):
            self.budget('negatives')
            raw = bytearray()
            try:
                if method == 'GET':
                    sock.sendall(b'x')
                else:
                    sock.shutdown(socket.SHUT_WR)
                while part := sock.recv(65536):
                    raw.extend(part)
                    require(len(raw) <= 65536, 'negative response bound')
                checked_error(bytes(raw), 400, 'unexpected_body' if method == 'GET' else 'invalid_request',
                              self.transport)
                self.wires.append(dict(file=dict(path=str(self.folder/(label+'-wire.bin'))),
                                       envelopes=1, success=False))
            finally:
                (self.folder/(label+'-wire.bin')).write_bytes(raw)
                sock.close()
        self.held.clear()
        h.save(self.folder/'partial-body-window.json', dict(seconds=time.monotonic()-self.hold_started))
        self.hold_started = None

    def business(self):
        caps = self.http('GET', '/v1/capabilities')
        h.save(self.folder/'capabilities.json', caps)
        require(caps['status'] == 200 and json.loads(caps['raw'])['observability']['version'] == 4,
                'new implementation contract')
        before = self.collect('before')['metrics']
        self.start_prometheus()
        body = make_request([dict(role='user', content=
            '从1开始逐行写出整数直到10000，每行一个，不要省略，不要解释，持续输出。')], [],
            stream=True, max_tokens=4096, timeout_ms=180000)
        holder = self.submit('holder', body, client=FullWireClient(self.port, self.folder, 'holder'))
        wire = self.folder/'holder-wire.bin'
        self.wait('holder-running', lambda m: m['q4t_active_chats'] == 1 and
                  m['q4t_seq_slots_free'] == 0 and content_bytes(wire) > 0)
        for i in range(127):
            self.hold(f'business-{i:03d}', 'POST', '/v1/chat/completions')
        full = self.wait('business-full', lambda m: m['q4t_business_connections'] == 128)
        require(full['q4t_observation_connections'] in (1, 2), 'separate scrape owner')
        overflow = self.http('GET', '/business-overflow')
        h.save(self.folder/'business-overflow.json', overflow)
        base.error_contract(overflow, 503, 'capacity_exhausted')
        for path in ('/metrics', '/v1/config', '/v1/capabilities', '/v1/models'):
            value = self.http('GET', path)
            h.save(self.folder/('full'+path.replace('/', '-')+'.json'), value)
            require(value['status'] == 200, 'observation blocked by business: '+path)
            if path == '/metrics':
                contracts.parse_metrics(value['raw'], version=4)
        self.health('business-full')
        diagnosed = self.collect('business-full', 'before')
        require(diagnosed['metrics']['q4t_business_connections'] == 128, 'diagnostic was not saturated')
        self.saturated_scrape()
        self.stop_prometheus()  # No background scrape may race the full observation partition.
        for i, path in enumerate(('/metrics', '/v1/config', '/v1/capabilities', '/v1/models')):
            self.hold(f'observation-{i}', 'GET', path)
        samples, end = [], time.monotonic()+2
        while True:
            value = self.http('GET', '/metrics')
            samples.append(value)
            if value['status'] == 503:
                base.error_contract(value, 503, 'observation_capacity_exhausted')
                break
            require(value['status'] == 200 and time.monotonic() < end, 'observation partition not full')
            time.sleep(.05)
        h.save(self.folder/'observation-full-probes.json', samples)
        self.collect('observation-full', 'before')
        self.health('observation-full')
        require(holder.thread.is_alive() and content_bytes(wire) > 0, 'active generation ended before cancellation')
        h.save(self.folder/'active-output.json', dict(content_bytes=content_bytes(wire), active_before_cancel=True))
        self.cancel(holder, 'holder')
        self.release_held()
        self.drain('after-release')
        self.normal(self.submit('ordinary-recovery', base.ordinary()))
        self.control()  # The SDK's explicit capabilities discovery.
        self.project_tool()
        self.drain('after-business')
        after = self.collect('after', 'before')
        check_accounting(before, after['metrics'])
        require(after['comparison']['state'] == 'available', 'lost same-instance evidence')
        self.health('final')
        h.save(self.out/'accounting.json', dict(passed=True, before=before, after=after['metrics'],
                                               comparison=after['comparison']))

    def run(self):
        result = dict(status='FAILED')
        try:
            self.start_model()
            self.business()
            require(self.counts == stage.PLANNED, 'frozen real counts')
            for record in self.wires:
                record['file'] = h.identity(record['file']['path'])
            h.save(self.out/'wire-schema-result.json', dict(passed=True, records=self.wires))
            result.update(status='PASS_BOUNDED_OBSERVATION_CAPACITY', automatic_retries=0,
                performance_acceptance=False, unclassified_header_flood_isolated=False,
                diagnostics=['HEALTHY', 'WARNING', 'UNKNOWN', 'WARNING'],
                saturated_scrape=True, physical_ram_hard_limit_proved=False)
        except BaseException as error:
            result['error'] = repr(error)
            if isinstance(error, stage.a.EnvironmentAbort):
                result['status'] = 'ENV_ABORT'
            raise
        finally:
            try:
                for _, sock, _ in self.held:
                    sock.close()
                self.held.clear()
                self.hold_started = None
                try:
                    self.stop_prometheus()
                finally:
                    self.stop_model()
                stage.a.require_idle_gpu(self.out/'gpu-after.json')
            except BaseException as error:
                result.update(status='ENV_ABORT' if isinstance(error, stage.a.EnvironmentAbort) else 'FAILED',
                              cleanup_error=repr(error))
                raise
            finally:
                result.update(counts=self.counts, elapsed_seconds=time.monotonic()-self.started,
                    control_request_budget_used=self.control_count,
                    control_count_includes_35_reserved_prometheus_scrapes=True)
                h.save(self.out/'result.json', result)
