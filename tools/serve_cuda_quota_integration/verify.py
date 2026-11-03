"""Production wrappers, bounded HTTP quality and owned process custody."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import threading
import time

from tools.serve_cuda_quota_integration import common as c
from tools.serve_cuda_quota import correction as custody
from tools.serve_service import acceptance as client, supervisor
from tools.serve_backpressure.verify import metrics as parse_metrics


def quota_metrics(metrics, enabled=True):
    get = lambda key: metrics['q4t_cuda_quota_' + key]
    c.require(get('enabled') == int(enabled), 'quota enabled metric')
    charged = get('charged_bytes')
    c.require(charged == sum(get(name + '_bytes') for name in
        ('reserved', 'live', 'releasing', 'pending', 'unknown')), 'quota partition')
    c.require(0 <= charged <= get('peak_bytes') <= get('limit_bytes'),
              'quota bounds')
    c.require(get('poisoned') == get('unknown_bytes') == 0, 'unknown quota state')
    c.require(get('entry_capacity') == 16384 and get('stream_capacity') == 256,
              'quota capacity discovery')
    for name in ('byte_rejections', 'entry_rejections', 'stream_rejections',
                 'contract_failures', 'backend_failures'):
        c.require(get(name + '_total') == 0, 'unexpected runtime refusal: ' + name)
    return {key: value for key, value in metrics.items()
            if key.startswith('q4t_cuda_quota_')}


def parse_probe(mode, text):
    rows = [json.loads(line) for line in text.splitlines()]
    c.require(rows and rows[0]['event'] == 'device' and
        rows[0]['sm_major'] == 11 and rows[0]['sm_minor'] == 0 and
        rows[0]['integrated'] == 1, 'native device')
    sizes = dict(mixed=[8, 8, 8, 8], async_=[8, 8], stream_reuse=[8, 8],
                 startup=[4, 8, 4], startup_reject=[], disabled=[8])[
                     'async_' if mode == 'async' else mode]
    verified = [row for row in rows if row['event'] == 'verified']
    c.require([row['bytes'] for row in verified] == [n * 1024**2 for n in sizes]
        and all(row['full_buffer'] for row in verified), 'native full buffers')
    expected = dict(
        mixed=[('mixed_full', 32, 0), ('mixed_closed', 0, 0)],
        async_=[('async_pending', 16, 8), ('other_no_credit', 16, 8),
                ('matching_credit', 8, 0), ('device_closed', 0, 0)],
        stream_reuse=[('async_pending', 16, 8), ('other_no_credit', 16, 8),
                ('new_generation_no_credit', 16, 8), ('device_closed', 0, 0)],
        startup=[('startup_reserved', 16, 0), ('startup_handed_once', 16, 0),
                 ('startup_closed', 0, 0)],
        startup_reject=[('startup_rollback', 0, 0)],
        disabled=[('explicitly_disabled', 0, 0)])[ 'async_' if mode == 'async' else mode]
    actual = [row for row in rows if row['event'] == 'quota']
    c.require(len(actual) == len(expected), 'native checkpoints')
    for row, (label, charged, pending) in zip(actual, expected):
        c.require(row['label'] == label and row['charged'] == charged * 1024**2
            and row['pending'] == pending * 1024**2 and
            row['charged'] == row['live'] + row['pending'] and
            0 <= row['charged'] <= row['peak'] <= row['limit'] and
            row['unknown'] == 0 and row['poisoned'] is False and
            row['enabled'] == (mode != 'disabled'), 'native quota: ' + label)
    end = rows[-1]
    c.require(end == dict(event='result', mode=mode, passed=True,
        verified_buffers=len(sizes), verified_bytes=sum(sizes) * 1024**2,
        checkpoints=len(expected)), 'native terminal')
    return end


class Children:
    """Keep short children waitable across a fresh production guard scan."""
    def __init__(self, lease):
        self.lease = lease
        self.children = []
        self.started = []

    def launch(self, folder, argv, env=None):
        folder.mkdir()
        c.save(folder / 'command.json', dict(argv=argv))
        with (folder / 'stdout.log').open('xb') as out, \
             (folder / 'stderr.log').open('xb') as err:
            process = subprocess.Popen(argv, cwd=c.ROOT, stdout=out, stderr=err,
                                       env=env)
        info = dict(custody.proc(process.pid), argv=argv,
                    registered_monotonic=time.monotonic())
        self.children.append((process, folder))
        self.started.append(info)
        c.save(folder / 'identity.json', info)
        return process

    @staticmethod
    def running(process):
        fd = os.pidfd_open(process.pid)
        try:
            return not select.select([fd], [], [], 0)[0]
        finally:
            os.close(fd)

    def wait(self, process, folder, timeout, expected=0):
        code = custody.wait_without_reaping(process, timeout)
        c.save(folder / 'waitable.json', dict(returncode=code,
            identity=custody.proc(process.pid), monotonic=time.monotonic()))
        c.require(code == expected, 'child exit: ' + str(folder))

    @staticmethod
    def signal(process, value):
        fd = os.pidfd_open(process.pid)
        try:
            signal.pidfd_send_signal(fd, value)
        finally:
            os.close(fd)

    def reap(self, output):
        until = time.monotonic() + 8
        ack = None
        while time.monotonic() < until:
            current = [custody.proc(p.pid) for p, _ in self.children]
            for before, now in zip(self.started, current):
                c.require(now['state'] == 'Z' and now['pid'] == before['pid'] and
                    now['start_ticks'] == before['start_ticks'] and
                    now['ppid'] == os.getpid() and now['pgid'] == os.getpgrp(),
                    'retained process identity drift')
            ack = custody.acknowledgement(
                custody.complete_lines(self.lease / 'events.jsonl'),
                custody.complete_lines(self.lease / 'nvml-queries.jsonl'),
                self.started[-1]['registered_monotonic'])
            if ack:
                break
            time.sleep(.05)
        c.require(ack is not None, 'guard acknowledgement timeout')
        c.save(output / 'custody.json', dict(acknowledgement=ack, retained=current))
        for process, folder in self.children:
            code = process.wait(timeout=1)
            c.save(folder / 'reaped.json', dict(pid=process.pid, returncode=code))
            c.require(not Path(f'/proc/{process.pid}').exists(), 'unreaped child')
        self.children.clear()

    def cleanup(self):
        # Always restrict termination to retained pidfds / our exact children.
        for process, folder in self.children:
            if self.running(process):
                self.signal(process, signal.SIGTERM)
        for process, folder in self.children:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.signal(process, signal.SIGKILL)
                process.wait(timeout=5)
        self.children.clear()


class Run:
    complete = client.Run.complete
    plain = client.Run.plain
    tools = client.Run.tools

    def __init__(self, plan, output):
        self.plan, self.out = plan, output
        output.mkdir()
        self.port = plan['port']
        client.OUT = output
        self.started = time.monotonic()
        self.counts = dict(generations=0, controls=0, starts=0, native=0)
        self.count_mutex = threading.Lock()
        self.children = Children(output.parent / 'lease')

    def budget(self, kind=None):
        c.require(time.time() < self.plan['started'] + 510, 'business deadline')
        if kind:
            with self.count_mutex:
                limits = dict(generations=7, controls=120, starts=3, native=6)
                c.require(self.counts[kind] < limits[kind], 'budget: ' + kind)
                self.counts[kind] += 1
                c.save(self.out / f'attempt-{kind}-{self.counts[kind]:03d}.json',
                       self.counts.copy())

    def get(self, label, path):
        self.budget('controls')
        value = supervisor.transport.get(self.port, path)
        c.save(self.out / (label + '.json'), value)
        c.require(value['http_status'] == 200, 'control unavailable: ' + path)
        return value

    def launch(self, label, extra):
        self.budget('starts')
        folder = self.out / label
        argv = [self.plan['binary']['path'], 'serve', '--model-dir',
            self.plan['model_dir'], '--host', '127.0.0.1', '--port', str(self.port),
            '--max-prefill', '2048', '--max-len', '8192',
            '--max-inflight-requests', '8', '--max-queued-requests', '2', *extra]
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        return self.children.launch(folder, argv, env), folder

    def ready(self, process, folder):
        until = time.monotonic() + 180
        while 'serving on port' not in (folder / 'stderr.log').read_text():
            # The current runner logs its readiness on stderr. Preserve both
            # streams; a different logging contract must fail this harness.
            self.budget()
            c.require(self.children.running(process) and time.monotonic() < until,
                      'serve startup failed or timed out')
            time.sleep(.2)

    def configuration(self, label, limit, sequences):
        raw = json.loads(self.get(label + '-config', '/v1/config')['body'])
        from jsonschema import Draft202012Validator
        Draft202012Validator(c.read(c.ROOT / 'docs/serve-config-v1.schema.json')).validate(raw)
        c.require(raw['protocol_source_sha256'] == self.plan['protocol_sha256'] and
            raw['max_sequences']['effective'] == sequences, 'configuration identity')
        quota = raw['cuda_allocation_quota']
        c.require(quota['enabled'] and quota['requested_mib'] == limit and
            quota['effective_limit_bytes'] > 0 and
            quota['physical_ram_limit'] is False and
            quota['no_budget_bypasses_quota'] is False, 'effective quota')
        if limit != 'auto':
            c.require(quota['effective_limit_bytes'] == limit * 1024**2,
                      'explicit quota bytes')
        c.save(self.out / (label + '-effective.json'), raw)
        caps = json.loads(self.get(label + '-capabilities', '/v1/capabilities')['body'])
        c.require(caps['cuda_allocation_quota']['profile'] == 'q4t-linked-cuda-quota-v1',
                  'quota discovery')
        return raw

    def idle(self, label, requests, sequences):
        until = time.monotonic() + 5
        while True:
            raw = self.get(label + '-metrics-' + str(self.counts['controls']), '/metrics')
            values = parse_metrics(raw['body'])
            if all(values[name] == 0 for name in client.EMPTY):
                break
            c.require(time.monotonic() < until, 'owners not drained')
            time.sleep(.05)
        c.require(values['q4t_seq_slots_free'] == values['q4t_seq_slots_total'] == sequences
            and values['q4t_requests_success_total'] == values['q4t_requests_total'] == requests
            and values['q4t_structured_workers_started_total'] ==
                values['q4t_structured_workers_reaped_total'], 'request accounting')
        quota_metrics(values)
        c.save(self.out / (label + '-metrics.json'), values)
        return values

    def stop(self, process, folder):
        c.save(folder / 'before-stop.json', dict(proc=custody.proc(process.pid),
            status=Path(f'/proc/{process.pid}/status').read_text(),
            meminfo=Path('/proc/meminfo').read_text()))
        self.children.signal(process, signal.SIGTERM)
        self.children.wait(process, folder, 45)
        text = (folder / 'stderr.log').read_text()
        c.require('shutdown complete (0 in-flight remaining)' in text,
                  'shutdown did not drain')
        reports = [json.loads(line.split('[q4t][cuda-quota] ', 1)[1])
            for line in text.splitlines() if '[q4t][cuda-quota] ' in line]
        c.require(reports[-1]['event'] == 'server_destroyed' and
            reports[-1]['poisoned'] is False and reports[-1]['unknown_bytes'] == 0,
            'final quota report')
        c.save(folder / 'quota-reports.json', reports)

    def run(self):
        try:
            for mode in c.MODES:
                self.budget('native')
                folder = self.out / ('native-' + mode)
                child = self.children.launch(folder,
                    [self.plan['native_probe']['path'], mode, str(os.getpid())])
                self.children.wait(child, folder, 15)
                c.save(folder / 'result.json', parse_probe(mode,
                    (folder / 'stdout.log').read_text()))
            folder = self.out / 'lifecycle'
            process = self.children.launch(folder, [self.plan['lifecycle_probe']['path']])
            self.children.wait(process, folder, 90)
            c.require('25 tests, 25 passed, 0 failed, 0 skipped' in
                (folder / 'stdout.log').read_text(), 'HTTP lifecycle contracts')
            with socket.socket() as test:
                test.bind(('127.0.0.1', self.port))
            process, folder = self.launch('startup-reject',
                ['--max-seq', '1', '--cuda-allocation-limit-mib', '1'])
            self.children.wait(process, folder, 90, expected=1)
            raw = (folder / 'stderr.log').read_text()
            reports = [json.loads(line.split('[q4t][cuda-quota] ', 1)[1])
                for line in raw.splitlines() if '[q4t][cuda-quota] ' in line]
            c.require('serving on port' not in raw and reports and
                reports[-1]['byte_rejections'] == 1 and
                reports[-1]['charged_bytes'] == reports[-1]['unknown_bytes'] == 0,
                'startup did not reject by quota with clean rollback')
            c.save(folder / 'quota-reports.json', reports)
            process, folder = self.launch('auto', ['--max-seq', '1'])
            self.ready(process, folder)
            auto = self.configuration('auto', 'auto', 1)
            c.require(auto['runtime']['mtp_enabled'] and auto['runtime']['mtp_k'] == 3,
                      'default MTP profile')
            self.plain('ordinary-json')
            self.plain('ordinary-sse', stream=True)
            value = self.complete('strict-json', client.strict_json())
            c.require(value.finish_reason == 'stop' and
                json.loads(value.message['content']) == {'ok': True}, 'strict quality')
            self.tools('project')
            self.idle('auto', 5, 1)
            self.get('auto-ready', '/readyz')
            self.stop(process, folder)
            # Fixed positive policy, deliberately independent of startup plan
            # and budget. The candidate's auto limit must exceed this profile's
            # existing live estimate; rejection is a failure, never a retry.
            limit = self.plan['explicit_limit_mib']
            process, folder = self.launch('explicit', ['--max-seq', '2', '--no-mtp',
                '--startup-plan', 'off', '--no-budget',
                '--cuda-allocation-limit-mib', str(limit)])
            self.ready(process, folder)
            explicit = self.configuration('explicit', limit, 2)
            c.require(not explicit['runtime']['mtp_enabled'] and
                not explicit['runtime']['memory_budget_enabled'], 'explicit profile')
            barrier = threading.Barrier(2)
            def request(index):
                barrier.wait(timeout=3)
                self.plain('concurrent-' + str(index), stream=bool(index))
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(request, i) for i in range(2)]
                for job in jobs:
                    job.result()
            self.idle('explicit', 2, 2)
            self.get('explicit-ready', '/readyz')
            self.stop(process, folder)
            self.children.reap(self.out)
            c.require(self.counts['generations'] == 7 and self.counts['starts'] == 3
                and self.counts['native'] == 6, 'stage completeness')
            client.require_idle(self.out / 'gpu-after.json')
            c.save(self.out / 'result.json', dict(status='PASS_BOUNDED_CUDA_QUOTA',
                counts=self.counts, elapsed_seconds=time.monotonic() - self.started,
                cleanup_confirmed=True, performance_acceptance=False,
                physical_ram_hard_limit=False, long_run_executed=False))
        except BaseException as error:
            c.save(self.out / 'failed.json', dict(error=repr(error), counts=self.counts,
                elapsed_seconds=time.monotonic() - self.started))
            raise
        finally:
            self.children.cleanup()
