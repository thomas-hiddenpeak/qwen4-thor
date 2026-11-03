"""Fixed one-hour workload, two exit modes and an actual default-pair roundtrip."""
from dataclasses import asdict
import copy
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time

from tools.serve_lifecycle import stage, deployment, observe
from tools.serve_admission import verify as base
from tools.serve_pressure.verify import FullWireClient, content_bytes
from tools.serve_ops import diagnose, bundle
from tools.serve_memory.verify import memory_contract
from isolated_scope import IsolatedScope
from chat_client import ProtocolError, RemoteError, make_request, read_sse
import tool_recovery
from jsonschema import Draft202012Validator

h, ROOT, require = stage.h, stage.ROOT, stage.h.require


def terminal(raw, *, graceful, validator):
    """A graceful error is complete; a crash prefix is never a successful turn."""
    values = []
    for line in raw.splitlines(keepends=True):
        if line.startswith(b'data: {') and line.endswith(b'\n'):
            value = json.loads(line[6:]);validator.validate(value);values.append(value)
    require(values, 'no captured SSE envelopes')
    require(all(choice.get('finish_reason') is None for value in values
                for choice in value.get('choices', [])), 'interrupted request claimed success')
    require(not any(value.get('usage') is not None for value in values), 'interrupted request claimed usage terminal')
    errors = [value for value in values if 'error' in value]
    if graceful:
        require(len(errors) == 1 and values[-1] == errors[0] and
                errors[0]['error']['code'] == 'server_shutdown' and
                raw.count(b'data: [DONE]') == 1 and raw.rstrip().endswith(b'data: [DONE]'),
                'graceful shutdown lacks one complete server_shutdown terminal')
        try:read_sse(io.BytesIO(raw))
        except RemoteError as error:
            require(error.code == 'server_shutdown', 'incorrect client shutdown classification')
        else:raise RuntimeError('client accepted shutdown as success')
    else:
        require(not errors and b'data: [DONE]' not in raw, 'crash stream unexpectedly complete')
        try:read_sse(io.BytesIO(raw))
        except (ProtocolError, UnicodeError):pass
        else:raise RuntimeError('client accepted crash prefix')
    return dict(envelopes=len(values), terminal_complete=graceful,
                outcome='SERVER_SHUTDOWN' if graceful else 'REQUEST_OUTCOME_UNKNOWN', success=False)


def accounting(rows, successes, workers):
    require(rows['q4t_requests_total'] == rows['q4t_requests_success_total'] == successes and
            rows['q4t_requests_error_total'] == rows['q4t_requests_aborted_total'] == 0 and
            rows['q4t_structured_workers_started_total'] ==
            rows['q4t_structured_workers_reaped_total'] == workers,
            'successful business/worker accounting differs')


class JournalClient(FullWireClient):
    """Durable submission intent and actual bytes; no response on interruption."""
    def __init__(self, port, folder, label, body, capabilities):
        super().__init__(port, folder, label)
        self.journal = folder/(label+'-journal')
        self.journal.mkdir(mode=0o700)
        tool_recovery.save_receipt(self.journal/'run-started.json', dict(journal_version=1,
            initial_messages=body['messages'], max_requests=1, max_tool_calls=1, model=body['model']))
        tool_recovery.save_receipt(self.journal/'capabilities.json', capabilities)
        tool_recovery.save_receipt(self.journal/'round-00-request.json', body)

    def complete(self, body, *, sink):
        with tool_recovery.journal_lock(self.journal, create=True):
            tool_recovery.save_receipt(self.journal/'round-00-submitted.json',
                                      dict(request_id=body['request_id']))
            with (self.journal/'round-00-wire.bin').open('xb', buffering=0) as wire:
                def capture(raw):
                    wire.write(raw);os.fsync(wire.fileno());sink(raw)
                result = super().complete(body, sink=capture)
            tool_recovery.save_receipt(self.journal/'round-00-response.json', asdict(result))
            return result


class Run(base.Run):
    def __init__(self, plan, output):
        super().__init__(copy.deepcopy(plan), output)
        self.deadline = plan['started']+stage.LIMITS['seconds']-90
        self.control_count = self.preflights = 0
        self.monitor = None
        self.quiescent, self.instances, self.interruptions = [], [], []
        self.request_ids, self.cancel_tokens = set(), set()
        self.diag_root = output/'diagnostics';self.diag_root.mkdir(mode=0o700)
        self.last_diag = None
        self.releases = deployment.verify_bundle(stage.BUNDLE, ROOT)
        self.soak_started = None

    def budget(self, kind=None):
        require(time.time() < self.deadline, 'original lifecycle deadline')
        if self.monitor:self.monitor.check()
        if kind:
            require(self.counts[kind] < stage.LIMITS[kind], 'stage attempts limit: '+kind)
            self.counts[kind] += 1
            h.save(self.out/f'attempt-{kind}-{self.counts[kind]:04d}.json',
                   dict(kind=kind, counts=self.counts.copy(), time=time.time()))

    def control(self, amount=1):
        self.budget()
        require(type(amount) is int and amount > 0 and
                self.control_count+amount <= stage.LIMITS['observations'], 'control observation budget')
        self.control_count += amount

    def http(self, method, path, body=None, *, timeout=5):
        self.control()
        return super().http(method, path, body, timeout=timeout)

    def new_folder(self, name):
        self.folder = self.out/name;self.folder.mkdir(mode=0o700)

    def wait(self, label, predicate, *, timeout=10):
        end, samples = time.monotonic()+timeout, []
        try:
            while True:
                rows = self.rows();samples.append(dict(monotonic=time.monotonic(), metrics=rows))
                if predicate(rows):return rows
                require(self.server.poll() is None and time.monotonic() < end,
                        'observation timed out: '+label)
                time.sleep(.1)
        finally:h.save(self.folder/(label+'-samples.json'), samples)

    def drain(self, label):
        observed_business = 0 if self.version == 4 else 1
        def empty(m):
            return (all(m[k] == 0 for k in base.EMPTY) and
                m['q4t_seq_slots_free'] == m['q4t_seq_slots_total'] == 1 and
                m['q4t_business_connections'] == observed_business and
                m['q4t_control_connections'] == 0 and
                (self.version == 3 or m['q4t_observation_connections'] == 1))
        rows = self.wait(label, empty, timeout=20)
        require(rows['q4t_structured_workers_started_total'] ==
                rows['q4t_structured_workers_reaped_total'], 'worker not reaped')
        h.save(self.folder/(label+'-resources.json'), dict(metrics=rows,
            separate_resource_observations=observe.sample(self.server, self.expected_start)))
        return rows

    def submit(self, label, body, *, client=None):
        require(body['request_id'] not in self.request_ids and body['cancel_token'] not in self.cancel_tokens,
                'never reuse request credentials across instances')
        self.request_ids.add(body['request_id']);self.cancel_tokens.add(body['cancel_token'])
        return super().submit(label, body, client=client)

    def project_tool(self):
        self.control()  # The JS bridge performs exactly one explicit discovery.
        super().project_tool()

    def preflight(self):
        require(self.preflights < stage.LIMITS['preflights'], 'preflight budget')
        self.preflights += 1
        body = dict(request=base.ordinary(), include_token_ids=True)
        self.schemas['preflight'].validate(body)
        h.save(self.folder/'preflight-request.json', body)
        response = self.http('POST', '/v1/chat/preflight', body)
        h.save(self.folder/'preflight-response.json', response)
        require(response['status'] == 200, 'preflight failed')
        Draft202012Validator(h.read(ROOT/'docs/serve-preflight-v1.schema.json')).validate(
            json.loads(response['raw']))

    def strict(self, pending):
        value = pending.finish(timeout=10);self.check_wire(pending)
        require(value.finish_reason == 'stop' and json.loads(value.message['content']) == {'ok':True},
                'strict business failed')

    def collect(self, label, *, cross_instance=False, expected_successes=None):
        self.control(5)
        previous = bundle.read(self.last_diag) if self.last_diag else None
        value = diagnose.snapshot(self.port, self.server.pid, self.runtime['sidecar'],
            log_path=self.model_folder/'server.log', previous_path=self.last_diag)
        output = self.diag_root/label;bundle.write(output, value)
        require(value['state'] == 'HEALTHY' and value['identity']['state'] == 'verified' and
                value['observability_version'] == self.version, 'live diagnostic failed')
        comparison = value['comparison']
        if cross_instance:
            require(previous is not None and comparison['state'] == 'unavailable' and
                    comparison['reason'] == 'instance_or_contract_changed' and not comparison['counters'],
                    'cross-instance counters incorrectly comparable')
        elif previous is not None:
            require(comparison['state'] == 'available', 'same-instance comparison unavailable')
            if expected_successes is not None:
                require(comparison['counters']['q4t_requests_success_total'] == expected_successes,
                        'diagnostic success delta differs')
        self.last_diag = output
        return value

    def sleep_until(self, until):
        while time.monotonic() < until:
            self.budget()
            require(self.server.poll() is None, 'model exited during observation')
            if self.soak_started is not None:
                require(time.monotonic()-self.soak_started <= stage.SOAK_MAX, 'frozen soak upper bound')
            time.sleep(min(.25, max(0, until-time.monotonic())))

    def soak(self):
        self.soak_started = time.monotonic();next_cycle = self.soak_started
        for cycle in range(1, stage.CYCLES+1):
            self.sleep_until(next_cycle)
            started = time.monotonic();next_cycle = started+stage.INTERVAL
            self.new_folder(f'cycle-{cycle:03d}')
            self.preflight()
            plain = self.submit('ordinary-json', base.ordinary())
            stream = self.submit('ordinary-sse', base.ordinary(stream=True))
            strict = self.submit('strict', base.constrained(stream=bool(cycle % 2)))
            self.normal(plain);self.normal(stream);self.strict(strict)
            self.project_tool()
            rows = self.drain('cycle-drained');accounting(rows, cycle*5, cycle*3)
            memory_contract(rows, opened=True, floor=2048)
            self.health('cycle')
            time.sleep(.05)
            sample = observe.sample(self.server, self.expected_start)
            self.quiescent.append(sample);h.save(self.folder/'quiescent.json', sample)
            h.save(self.folder/'timing.json', dict(started=started, ended=time.monotonic(),
                seconds_since_soak_start=time.monotonic()-self.soak_started))
            require(time.monotonic()-self.soak_started < stage.SOAK_MAX, 'frozen soak upper bound')
            if cycle % 60 == 0:self.collect(f'cycle-{cycle:03d}', expected_successes=300)
            print(f'cycle {cycle}/{stage.CYCLES} PASS; {time.monotonic()-self.soak_started:.1f}s', flush=True)
        self.sleep_until(self.soak_started+stage.SOAK_SECONDS)
        elapsed = time.monotonic()-self.soak_started
        require(stage.SOAK_SECONDS <= elapsed <= stage.SOAK_MAX, 'fixed soak duration')
        h.save(self.out/'resource-envelope.json', observe.plateau(self.quiescent))
        h.save(self.out/'soak-result.json', dict(passed=True, cycles=stage.CYCLES,
            seconds=elapsed, generations=stage.CYCLES*5, preflights=self.preflights,
            instance_pid=self.server.pid, instance_start_ticks=self.expected_start,
            automatic_retries=0, throughput_slo_claim=False, day_scale_stability_proved=False))
        self.soak_started = None

    def release_config(self, role):
        require(deployment.state(ROOT, self.releases)['role'] == role, 'wrong installed role')
        require(not list((ROOT/'build').glob('*recovery*.json')), 'incomplete deployment')
        return dict(binary=str(ROOT/'build/q4t'), sidecar=ROOT/'build/q4t.release.json',
                    pair=self.releases['releases'][role]['pair'],
                    installed=deployment.state(ROOT, self.releases),
                    rules=self.releases['releases'][role]['rules'])

    def start_model(self, name, role, *, cross_instance=False):
        self.budget('starts');h.require_guard(Path(self.plan['lease_root']))
        require(self.server is None, 'previous instance not closed')
        self.role = role;self.runtime = self.release_config(role)
        pair = self.runtime['pair']
        self.version = pair['observability_version']
        self.plan['protocol_sha256'] = pair['protocol_sha256']
        self.config_schema = Draft202012Validator(h.read(stage.PACKAGE/f'config-v{self.version}.schema.json'))
        self.new_folder(name);self.model_folder = self.folder
        self.model_calls_begin = len(self.calls)
        stage.a.require_idle_gpu(self.model_folder/'gpu-before.json')
        argv = [self.runtime['binary'], 'serve', '--model-dir', self.plan['model_dir'],
            '--host', '127.0.0.1', '--port', str(self.port), '--max-prefill', '2048',
            '--max-len', '8192', '--max-seq', '1', '--max-inflight-requests', '8',
            '--max-queued-requests', '2', '--min-available-memory-mib', '2048']
        env = {k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        h.save(self.model_folder/'command.json', dict(argv=argv, installed=self.runtime['installed'],
            rules=self.runtime['rules'], rules_activation='selected identity; no monitoring daemon installed'))
        self.server = IsolatedScope(argv, cwd=ROOT, env=env, log_path=self.model_folder/'server.log',
            unit=f'q4t-lifecycle-{os.getpid()}-{self.counts["starts"]}.scope', memory_max=None,
            evidence=self.model_folder/'scope')
        self.expected_start = h.read(self.model_folder/'scope/identity.json')['start_ticks']
        self.monitor = observe.Monitor(self.server, self.expected_start, self.model_folder)
        end = time.monotonic()+240
        while 'serving on port' not in (self.model_folder/'server.log').read_text():
            self.budget()
            require(self.server.poll() is None and time.monotonic() < end, 'model startup failed')
            time.sleep(.25)
        config = self.http('GET', '/v1/config');h.save(self.folder/'configuration.json', config)
        value = json.loads(config['raw']);self.config_schema.validate(value)
        require(config['status'] == 200 and value['protocol_source_sha256'] == self.plan['protocol_sha256'] and
                value['runtime']['mtp_enabled'] and
                value['memory_admission']['min_available_mib']['effective'] == 2048, 'runtime identity')
        caps = self.http('GET', '/v1/capabilities');h.save(self.folder/'capabilities.json', caps)
        require(caps['status'] == 200, 'capabilities unavailable')
        self.capabilities = json.loads(caps['raw'])
        initial = self.drain('startup-drained');accounting(initial, 0, 0)
        self.health('startup');self.collect(name+'-start', cross_instance=cross_instance)
        print(f'{name} started role={role} observability=v{self.version}', flush=True)

    def recovery_business(self, name, old_requests=()):
        self.new_folder(name)
        for index, body in enumerate(old_requests):
            self.budget('negatives')
            request = {k:body[k] for k in ('request_id', 'cancel_token')}
            h.save(self.folder/f'old-{index}-cancel-request.json', request)
            value = self.http('POST', '/v1/requests/cancel', request)
            h.save(self.folder/f'old-{index}-cancel-response.json', value)
            require(value['status'] == 404 and json.loads(value['raw'])['error']['code'] ==
                    'request_not_found', 'old request registration leaked across restart')
            self.transport.validate(json.loads(value['raw']))
        self.normal(self.submit('ordinary', base.ordinary()))
        self.project_tool();rows = self.drain('recovered');accounting(rows, 3, 2)
        self.health('recovered');self.collect(name, expected_successes=3)

    def interrupted(self, name, *, crash):
        self.new_folder(name)
        body = make_request([dict(role='user', content=
            '从1开始逐行写出整数直到10000，每行一个，不要省略，不要解释，持续输出。')], [],
            stream=True, max_tokens=4096, timeout_ms=180000)
        def submit(label, body):
            client = (JournalClient(self.port, self.folder, label, body, self.capabilities) if crash else
                      FullWireClient(self.port, self.folder, label))
            return self.submit(label, body, client=client)
        holder = submit('active', body)
        self.wait('active-output', lambda m: m['q4t_seq_slots_free'] == 0 and
                  content_bytes(self.folder/'active-wire.bin') > 0)
        queued = submit('queued', base.constrained(stream=True))
        rows = self.wait('prepared-queue', lambda m: m['q4t_queued_requests'] == 1 and
            m['q4t_structured_workers_active'] == 1 and m['q4t_structured_preparing'] == 0 and
            m['q4t_seq_slots_free'] == 0)
        require(holder.thread.is_alive() and queued.thread.is_alive(), 'request ended before signal')
        snapshot = observe.sample(self.server, self.expected_start)
        children = {pid:start for pid,start in snapshot['process_memory']['process_start_ticks'].items()
                    if int(pid) != self.server.pid}
        require(len(children) == 1, 'one actual owned grammar worker required')
        h.save(self.folder/'before-signal.json', dict(metrics=rows, resources=snapshot, children=children))
        folder = self.folder
        self.stop_model(crash=crash, children=children, observations=folder,
                        expected_reaped=None if crash else int(rows['q4t_structured_workers_started_total']))
        for call in (holder, queued):
            require(call.value is None and call.error is not None and not call.thread.is_alive(),
                    'interrupted client claimed completion or remained active')
            if not crash:require(isinstance(call.error, RemoteError) and call.error.code == 'server_shutdown',
                                 'graceful client error classification')
            path = folder/(call.label+'-wire.bin')
            checked = terminal(path.read_bytes(), graceful=not crash, validator=self.transport)
            self.interruptions.append(dict(file=h.identity(path), **checked))
            if not crash:self.wires.append(dict(file=h.identity(path), **checked))
            else:
                journal = call.client.journal
                require((journal/'round-00-wire.bin').read_bytes() == path.read_bytes(), 'journal wire drift')
                before = {p.name:h.sha(p) for p in journal.iterdir()}
                recovery = tool_recovery.inspect_recovery(journal)
                h.save(folder/(call.label+'-recovery.json'), recovery)
                require(recovery['state'] == 'REQUEST_OUTCOME_UNKNOWN' and not recovery['automatic_retry'] and
                        recovery['completed_tool_calls'] == recovery['attempted_tool_calls'] == 0 and
                        {p.name:h.sha(p) for p in journal.iterdir()} == before, 'read-only unknown recovery')
        if not crash:
            log = (self.model_folder/'server.log').read_text()
            for call in (holder, queued):
                ends = [line for line in log.splitlines() if f'id={call.body["request_id"]} ' in line and
                        'event=end ' in line]
                require(len(ends) == 1 and 'reason=server_shutdown' in ends[0], 'shutdown request accounting')
        return [holder.body, queued.body]

    def stop_model(self, *, crash=False, children=None, observations=None, expected_reaped=None):
        require(self.server is not None, 'missing owned server')
        server, folder = self.server, self.model_folder
        result = dict(mode='SIGKILL' if crash else 'SIGTERM', passed=False,
                      pid=server.pid, start_ticks=self.expected_start)
        started = time.monotonic()
        try:
            if self.monitor:
                monitor, self.monitor = self.monitor, None
                monitor.close();result['continuous_samples'] = monitor.count
            require(server.poll() is None, 'server exited before owned signal')
            if crash:server.kill()
            else:server.terminate()
            result['returncode'] = server.wait(timeout=45)
            result['exit_seconds'] = time.monotonic()-started
            require(result['returncode'] == (-9 if crash else 0) and result['exit_seconds'] <= 45,
                    'unexpected model exit')
            if children:result['child_exit'] = observe.wait_children(children, observations)
            server.snapshot('payload-exited-before-cleanup')
            for call in self.calls[self.model_calls_begin:]:call.thread.join(timeout=5)
            require(all(not c.thread.is_alive() for c in self.calls[self.model_calls_begin:]), 'owned clients not joined')
            if not crash:
                log = (folder/'server.log').read_text()
                require('shutdown complete (0 in-flight remaining)' in log, 'incomplete server drain')
                if expected_reaped is not None:
                    reports = re.findall(r'grammar worker reaped=(\d+) ', log)
                    require(len(reports) == expected_reaped and all(v == '1' for v in reports),
                            'shutdown worker reap accounting')
            result['passed'] = True
        finally:
            try:
                if server.poll() is None:
                    server.kill();server.wait(timeout=10)
                server.close()
                result['scope_removed'] = server.closed
                if server.closed:self.server = None
                stage.a.require_idle_gpu(folder/'gpu-after.json')
            finally:
                result['elapsed_seconds'] = time.monotonic()-started
                h.save(folder/'shutdown.json', result)
                self.instances.append(result)
        require(result['passed'] and result['scope_removed'], 'owned lifecycle failed')

    def restore_current(self):
        value = deployment.state(ROOT, self.releases)
        marker = deployment.marker_path(ROOT)
        if value['role'] != 'current' or marker.exists():
            deployment.transact(ROOT, stage.BUNDLE, 'current', self.out/'failure-restore',
                                Path(self.plan['lease_root']), recovering=marker.exists())

    def run(self):
        result = dict(status='FAILED', automatic_retries=0, requests_replayed=0)
        try:
            self.start_model('model-1-soak', 'current')
            self.soak()
            graceful = self.interrupted('graceful-inflight', crash=False)
            self.start_model('model-2-restart', 'current', cross_instance=True)
            self.recovery_business('after-graceful', graceful)
            crashed = self.interrupted('crash-inflight', crash=True)
            self.start_model('model-3-restart', 'current', cross_instance=True)
            self.recovery_business('after-crash', crashed);self.stop_model(expected_reaped=2)
            self.budget()
            deployment.transact(ROOT, stage.BUNDLE, 'previous', self.out/'switch-previous',
                                Path(self.plan['lease_root']))
            self.start_model('model-4-rollback', 'previous', cross_instance=True)
            self.recovery_business('previous-business');self.stop_model(expected_reaped=2)
            deployment.transact(ROOT, stage.BUNDLE, 'current', self.out/'switch-current',
                                Path(self.plan['lease_root']))
            self.start_model('model-5-restored', 'current', cross_instance=True)
            self.recovery_business('current-business');self.stop_model(expected_reaped=2)
            require(self.counts == stage.PLANNED and self.preflights == stage.CYCLES and
                    sum(r['success'] for r in self.wires) == 1212 and len(self.interruptions) == 4,
                    'fixed lifecycle count differs')
            h.save(self.out/'wire-schema-result.json', dict(passed=True, complete_records=self.wires,
                interrupted_records=self.interruptions, complete_successes=1212,
                graceful_errors=2, unknown_crash_prefixes=2))
            result.update(status='PASS_BOUNDED_LIFECYCLE', successes=1212,
                graceful_shutdown_errors=2, unknown_crash_outcomes=2, fixed_cycles=stage.CYCLES,
                real_default_pair_roundtrip=True, final_default_changed=False,
                performance_acceptance=False, physical_ram_hard_limit_proved=False,
                day_scale_stability_proved=False, daemon_supervisor_deployed=False)
        except BaseException as error:
            result.update(status='ENV_ABORT' if isinstance(error, (observe.ObservationAbort,
                stage.a.EnvironmentAbort)) else 'FAILED', error=repr(error))
            raise
        finally:
            try:
                if self.server is not None:self.stop_model()
                self.restore_current()
                stage.installed()
            except BaseException as error:
                result.update(status='ENV_ABORT' if isinstance(error, (observe.ObservationAbort,
                    stage.a.EnvironmentAbort)) else 'FAILED', cleanup_error=repr(error))
                raise
            finally:
                result.update(counts=self.counts, preflights=self.preflights,
                    control_requests_used=self.control_count, instances=self.instances,
                    elapsed_seconds=time.monotonic()-self.started)
                h.save(self.out/'result.json', result)
