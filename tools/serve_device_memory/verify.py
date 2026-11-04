"""Frozen real HTTP business and simulated CUDA-status failure boundaries."""
import copy
import json
import os
import socket
import subprocess
import time

from tools.serve_device_memory import stage
from tools.serve_admission import verify as base
from tools.serve_startup_memory.verify import validate_records, startup_records
from tools.serve_lifecycle import observe
from isolated_scope import IsolatedScope

h, a, ROOT = stage.h, stage.a, stage.ROOT
FAULT_REQUEST = 'srv02f-device-fault'
SCENARIOS = (
    ('model', None, None, False, 0),
    ('optional-oom', 'request.prompt_trunk', 'oom', False, 0),
    ('session-oom', 'request.session_trunk', 'oom', True, 0),
    ('fatal-allocation', 'request.prompt_trunk', 'other', False, 1),
    ('early-release', 'request.prompt_trunk_free', 'other', False, 1),
    ('late-release', 'request.draft_trunk_free', 'other', False, 1),
    ('mtp-prepare', 'request.mtp_prepare', 'other', False, 1),
    ('startup', 'startup.scheduler_tokens', 'other', False, 1),
    ('shutdown', 'shutdown.prefill_logits', 'other', False, 1),
)


def fault_environment(site, kind):
    env = {k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    if site:
        env.update(Q4T_SERVE_DEVICE_FAULT=site, Q4T_SERVE_DEVICE_ERROR=kind)
        if site.startswith('request.'):
            env['Q4T_SERVE_REQUEST'] = FAULT_REQUEST
    return env


def fault_body(*, stream=False, session=False):
    body = base.ordinary(stream=stream)
    body['request_id'] = FAULT_REQUEST
    if session:
        body['session_id'] = 'c'*64
    return body


def cache_body():
    # Identical prompt/budget to the accepted thinking-budget private cache case.
    body = base.ordinary()
    body['messages'].insert(0, dict(role='developer', content='遵循用户请求。'))
    body['messages'][-1]['content'] = ('以下是无关占位记录，忽略其内容。\n' +
        '记录仅供占位。\n'*1400 + '\n现在只回复数字710003。')
    body.update(session_id='c'*64, thinking_token_budget=8, reasoning_field='reasoning',
        include_reasoning=True, chat_template_kwargs=dict(enable_thinking=True,
            reasoning_effort='low', preserve_thinking=True))
    return body


def error_terminal(raw, stream, code):
    values = base.wire_values(raw, stream)
    h.require(values and set(values[-1]) == {'error'} and
              values[-1]['error']['code'] == code, 'missing expected terminal error')
    h.require(sum('error' in value for value in values) == 1, 'duplicate error')
    if not stream:
        h.require(len(values) == 1, 'mixed JSON success and failure')
    else:
        h.require(raw.count(b'data: [DONE]') == 1 and raw.rstrip().endswith(b'data: [DONE]'),
                  'error stream needs one final DONE')
        for value in values[:-1]:
            choices = value.get('choices', [])
            h.require(value.get('object') == 'chat.completion.chunk' and
                value.get('usage') is None and len(choices) == 1 and
                choices[0].get('finish_reason') is None, 'error stream published success/usage')
    return values


class Run(base.Run):
    def __init__(self, plan, output):
        super().__init__(plan, output)
        self.deadline = plan['started']+stage.LIMITS['seconds']-90
        self.controls = 0
        self.monitor = None

    def budget(self, kind=None):
        h.require(time.time() < self.deadline, 'frozen device stage deadline')
        if kind:
            h.require(self.counts[kind] < stage.LIMITS[kind], 'frozen attempts: '+kind)
            self.counts[kind] += 1
            h.save(self.out/f'attempt-{kind}-{self.counts[kind]:02d}.json',
                   dict(kind=kind, counts=self.counts.copy(), time=time.time()))

    def http(self, method, path, body=None, *, timeout=5):
        h.require(self.controls < stage.LIMITS['observations'], 'control observation budget')
        self.controls += 1
        return super().http(method, path, body, timeout=timeout)

    def wait(self, label, predicate, *, timeout=10):
        end, samples = time.monotonic()+timeout, []
        try:
            while True:
                self.budget()
                if self.monitor: self.monitor.check()
                rows = self.rows()
                samples.append(dict(monotonic=time.monotonic(), metrics=rows))
                if predicate(rows): return rows
                h.require(self.server.poll() is None and time.monotonic() < end, 'wait: '+label)
                time.sleep(.1)
        finally:
            h.save(self.folder/(label+'-samples.json'), samples)

    def stop_monitor(self):
        monitor, self.monitor = self.monitor, None
        if monitor: monitor.close()

    def success(self, label, body):
        call = self.submit(label, body)
        value = call.finish()
        h.require(value.finish_reason == 'stop' and value.message['content'].strip() == '710003',
                  'task quality: '+label)
        self.check_wire(call)
        return value

    def failure(self, body, code):
        self.budget('negatives')
        call = self.submit('fault', body)
        call.thread.join(timeout=180)
        h.require(not call.thread.is_alive() and call.value is None and
            isinstance(call.error, base.RemoteError) and call.error.code == code and
            call.error.status == (200 if body['stream'] else 503), 'client fault outcome')
        raw = (self.folder/'fault-wire.bin').read_bytes()
        values = error_terminal(raw, body['stream'], code)
        for value in values: self.transport.validate(value)
        self.check_wire(call, success=False)
        return values

    def cache_recovery(self):
        body = cache_body()
        first = self.success('cache-seed', body)
        h.require(first.usage['prompt_tokens'] >= 8192, 'cache minimum not exercised')
        body = copy.deepcopy(body)
        body['request_id'] = 'srv02f-cache-restored'
        body['messages'] += [copy.deepcopy(first.message),
                             dict(role='user', content='仍然只回复数字710003。')]
        cached = self.success('cache-restored', body)
        h.require(cached.usage.get('prompt_tokens_details', {}).get('cached_tokens', 0) >= 8192,
                  'real session restore missing')
        del body['session_id']
        body['request_id'] = 'srv02f-cache-fresh'
        fresh = self.success('cache-fresh', body)
        h.require(cached.message['content'] == fresh.message['content'] and
            cached.finish_reason == fresh.finish_reason and
            cached.usage['prompt_tokens'] == fresh.usage['prompt_tokens'], 'cache answer mismatch')
        h.save(self.folder/'cache-comparison.json', dict(passed=True,
            cached_tokens=cached.usage['prompt_tokens_details']['cached_tokens'],
            numerical_equivalence_claim=False))

    def production(self):
        self.normal(self.submit('ordinary-recovery', base.ordinary()))
        self.normal(self.submit('ordinary-sse', base.ordinary(stream=True)))
        call = self.submit('strict-json', base.constrained())
        value = call.finish(); self.check_wire(call)
        h.require(value.finish_reason == 'stop' and json.loads(value.message['content']) == {'ok':True},
                  'strict JSON task quality')
        h.require(self.controls < stage.LIMITS['observations'], 'SDK discovery reserve')
        self.controls += 1
        self.project_tool()
        rows = self.drain('production-complete')
        h.require(rows['q4t_requests_total'] == rows['q4t_requests_success_total'] == 5 and
            rows['q4t_requests_error_total'] == rows['q4t_requests_aborted_total'] == 0 and
            rows['q4t_structured_workers_started_total'] == rows['q4t_structured_workers_reaped_total'] == 3,
            'production request/worker accounting')

    def scenario(self, name, site, kind, cache, expected_exit):
        self.budget('starts')
        self.folder = self.out/name
        self.folder.mkdir()
        self.server = None
        a.require_idle_gpu(self.folder/'gpu-before.json')
        binary = self.plan['binary' if name == 'model' else 'validation_binary']
        argv = [binary['path'], 'serve', '--model-dir', self.plan['model_dir'], '--host', '127.0.0.1',
                '--port', str(self.port), '--max-prefill', '2048', '--max-len', '16384' if cache else '8192',
                '--max-seq', '1', '--max-inflight-requests', '8', '--max-queued-requests', '2']
        if cache:
            directory = self.folder/'private-cache';directory.mkdir(mode=0o700)
            argv += ['--seq-capacities', '16384', '--session-cache-dir', str(directory)]
        h.save(self.folder/'command.json', dict(argv=argv,binary=binary,site=site,kind=kind,
            simulated_api_status_only=True if site else False, expected_exit=expected_exit))
        passed = False
        try:
            self.server = IsolatedScope(argv,cwd=ROOT,env=fault_environment(site,kind),
                log_path=self.folder/'server.log', unit=f'q4t-device-{name}-{os.getpid()}.scope',
                memory_max=None,evidence=self.folder/'scope')
            start = h.read(self.folder/'scope/identity.json')['start_ticks']
            if name == 'startup':
                self.budget('negatives')
                rc = self.server.wait(timeout=240)
                log = (self.folder/'server.log').read_text()
                h.require(rc == 1 and 'serving on port' not in log and
                    'scheduler device memory failure' in log, 'startup failure did not stop startup')
            else:
                self.monitor = observe.Monitor(self.server,start,self.folder)
                end = time.monotonic()+240
                while 'serving on port' not in (self.folder/'server.log').read_text():
                    self.budget(); self.monitor.check()
                    h.require(self.server.poll() is None and time.monotonic()<end,'startup failed: '+name)
                    time.sleep(.25)
                validate_records(startup_records((self.folder/'server.log').read_text()),2048)
                response = self.http('GET','/v1/capabilities')
                h.save(self.folder/'capabilities.json',response)
                caps = json.loads(response['raw'])
                h.require(response['status']==200 and caps['device_memory']['release_before_success_terminal'] and
                    not caps['device_memory']['all_model_and_media_allocations_covered'], 'memory capability scope')
                response = self.http('GET','/v1/config')
                h.save(self.folder/'configuration.json',response)
                self.config_schema.validate(json.loads(response['raw']))
                h.require(response['status']==200 and json.loads(response['raw'])['protocol_source_sha256']==
                    self.plan['protocol_sha256'],'runtime protocol identity')
                self.drain('before-business')
                if name == 'model': self.production()
                elif name == 'optional-oom':
                    self.success('fallback',fault_body())
                    self.success('recovered',base.ordinary(stream=True))
                    rows = self.drain('recovered')
                    h.require(rows['q4t_requests_success_total']==2 and rows['q4t_requests_error_total']==0,
                              'optional OOM poisoned service')
                    log = (self.folder/'server.log').read_text()
                    h.require('trunk allocation exhausted' in log and '[mtp-accept]' in log,
                              'missing fallback and MTP recovery')
                elif name == 'session-oom':
                    self.failure(fault_body(stream=True,session=True),'device_memory_exhausted')
                    self.drain('after-oom')
                    self.cache_recovery()
                    rows = self.drain('cache-complete')
                    h.require(rows['q4t_requests_total']==4 and rows['q4t_requests_success_total']==3 and
                              rows['q4t_requests_error_total']==1, 'session OOM accounting')
                elif name == 'shutdown':
                    self.success('before-shutdown',base.ordinary())
                    self.drain('before-shutdown')
                    self.budget('negatives')
                else:
                    # Expected termination is observed by pidfd and scope counters;
                    # do not run a live-PID sampler across the intentional exit.
                    h.save(self.folder/'before-fatal-observation.json',observe.sample(self.server,start))
                    self.stop_monitor()
                    values = self.failure(fault_body(stream=name in ('early-release','late-release')),'gpu_unhealthy')
                    if name == 'late-release':
                        h.require(any(v.get('choices',[{}])[0].get('delta',{}).get('content') for v in values[:-1]),
                                  'late failure did not exercise a partial answer')
                    rc = self.server.wait(timeout=45)
                    h.require(rc == 1, 'fatal request left service running or exited cleanly')
                if expected_exit == 0 or name == 'shutdown':
                    self.health('final')
                    h.save(self.folder/'final-observation.json',observe.sample(self.server,start))
                self.stop_monitor()
            if self.server.poll() is None: self.server.terminate()
            rc = self.server.wait(timeout=45)
            log = (self.folder/'server.log').read_text()
            h.require(rc == expected_exit,'unexpected owned process exit: '+name)
            if site:
                h.require(log.count('[serve-test-device]')==1 and f'site={site}' in log,
                          'fault not exercised exactly once')
            else:
                h.require('[serve-test-device]' not in log,'production contains fault hook')
            if expected_exit==0:
                h.require('shutdown complete (0 in-flight remaining)' in log,'unclean stop')
            end = self.server.snapshot('payload_exited')
            observe.prior.check_oom(end['resources']['cgroup']['observations']['memory.events']['value'])
            h.save(self.folder/'result.json',dict(passed=True,returncode=rc,site=site,
                physical_oom_proved=False,simulated_api_status_only=bool(site)))
            passed = True
        finally:
            try:
                self.stop_monitor()
            finally:
                if self.server:
                    try:
                        if self.server.poll() is None:
                            self.server.terminate()
                            try:self.server.wait(timeout=45)
                            except subprocess.TimeoutExpired:
                                self.server.kill();self.server.wait(timeout=10)
                        if not passed:
                            h.save(self.folder/'cleanup.json',dict(returncode=self.server.poll(),accepted=False))
                    finally:self.server.close()
                for call in self.calls:
                    call.thread.join(timeout=10)
                    h.require(not call.thread.is_alive(),'owned client still running')
                a.require_idle_gpu(self.folder/'gpu-after.json')

    def run(self):
        result = dict(status='FAILED')
        try:
            with socket.socket() as sock:sock.bind(('127.0.0.1',self.port))
            for row in SCENARIOS:self.scenario(*row)
            h.require(self.counts==stage.PLANNED,'frozen attempt counts changed')
            h.save(self.out/'wire-schema-result.json',dict(passed=True,records=self.wires))
            result.update(status='PASS_BOUNDED_DEVICE_MEMORY',physical_oom_proved=False,
                full_allocation_coverage=False,performance_acceptance=False,no_retry=True)
        except BaseException as error:
            result.update(status='ENV_ABORT' if isinstance(error,(a.EnvironmentAbort,observe.ObservationAbort))
                          else 'FAILED',error=repr(error))
            raise
        finally:
            result.update(counts=self.counts,control_requests=self.controls,
                elapsed_seconds=time.monotonic()-self.started)
            h.save(self.out/'result.json',result)
