"""Immediate sequential structured requests on the actual production ELF."""
import json
import os
import socket
import subprocess
import time

from tools.serve_handoff import stage
from tools.serve_service import acceptance as client, supervisor
from tools.serve_lifecycle import observe
from isolated_scope import IsolatedScope

h, a, ROOT = stage.h, stage.a, stage.ROOT


class Run:
    complete = client.Run.complete
    plain = client.Run.plain
    tools = client.Run.tools

    def __init__(self, plan, output):
        self.plan, self.out, self.port = plan, output, plan['port']
        output.mkdir()
        client.OUT = output
        self.counts = dict(generations=0, negatives=0, controls=0, starts=0)
        self.started = time.monotonic()
        self.server = self.monitor = None

    def budget(self, kind=None):
        h.require(time.time() < self.plan['started']+stage.LIMITS['seconds']-90,
                  'handoff business deadline')
        if self.monitor: self.monitor.check()
        if kind:
            h.require(self.counts[kind] < stage.LIMITS[kind], 'attempt budget: '+kind)
            self.counts[kind] += 1
            h.save(self.out/f'attempt-{kind}-{self.counts[kind]:02d}.json', self.counts)

    def get(self, label, path):
        self.budget('controls')
        value = supervisor.transport.get(self.port, path)
        h.save(self.out/(label+'.json'), value)
        h.require(value['http_status'] == 200, 'control unavailable: '+path)
        return value

    def strict(self, label, stream=False):
        body = client.strict_json()
        body['stream'] = stream
        if stream: body['stream_options'] = dict(include_usage=True)
        value = self.complete(label, body)
        h.require(value.finish_reason == 'stop' and
                  json.loads(value.message['content']) == {'ok': True},
                  'strict output quality')

    def incomplete(self):
        self.budget('negatives')
        body = client.strict_json(); body['max_tokens'] = 1
        try:
            self.complete('incomplete', body)
        except RuntimeError as error:
            h.require(str(error) == 'generation HTTP failure', 'unexpected client error')
        else:
            raise RuntimeError('truncated constrained output reported success')
        envelope = h.read(self.out/'incomplete-http.json')
        value = json.loads((self.out/'incomplete-wire.bin').read_bytes())
        h.require(envelope['status'] == 502 and envelope['eof'] and
            value['error']['code'] == 'structured_output_incomplete', 'incorrect error terminal')

    def idle(self):
        # No drain/sleep occurs between the handoff requests. Only the final
        # resource audit can poll, within the independent control budget.
        until = time.monotonic()+5
        while True:
            response = self.get('final-metrics-'+str(self.counts['controls']), '/metrics')
            metrics, _ = client.contracts.parse_metrics(response['body'], version=4)
            if all(metrics[name] == 0 for name in client.EMPTY): break
            h.require(time.monotonic() < until, 'owners did not drain')
            time.sleep(.05)
        h.require(metrics['q4t_seq_slots_free'] == metrics['q4t_seq_slots_total'] == 1 and
            metrics['q4t_structured_workers_started_total'] ==
            metrics['q4t_structured_workers_reaped_total'] == 7 and
            metrics['q4t_requests_total'] == 9 and metrics['q4t_requests_success_total'] == 8 and
            metrics['q4t_requests_aborted_total'] == 0 and
            metrics['q4t_requests_error_total'] == 1 and
            metrics['q4t_structured_rejected_total'] == 0, 'resource/outcome accounting')
        h.save(self.out/'final-metrics.json', metrics)

    def run(self):
        result = dict(status='FAILED', automatic_request_retry=False, performance_acceptance=False)
        try:
            with socket.socket() as test: test.bind(('127.0.0.1', self.port))
            self.budget('starts')
            argv = [self.plan['binary']['path'], 'serve', '--model-dir', self.plan['model_dir'],
                '--host', '127.0.0.1', '--port', str(self.port), '--max-prefill', '2048',
                '--max-len', '8192', '--max-seq', '1', '--max-inflight-requests', '8',
                '--max-queued-requests', '2']
            h.save(self.out/'command.json', dict(argv=argv, binary=self.plan['binary']))
            env = {k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
            self.server = IsolatedScope(argv, cwd=ROOT, env=env,
                log_path=self.out/'server.log', unit=f'q4t-handoff-{os.getpid()}.scope',
                memory_max=None, evidence=self.out/'scope')
            start = h.read(self.out/'scope/identity.json')['start_ticks']
            self.monitor = observe.Monitor(self.server, start, self.out)
            until = time.monotonic()+240
            while 'serving on port' not in (self.out/'server.log').read_text():
                self.budget()
                h.require(self.server.poll() is None and time.monotonic() < until,
                          'candidate did not start')
                time.sleep(.25)
            config = client.contracts.configuration(json.loads(
                self.get('configuration', '/v1/config')['body']), version=4)
            h.require(config['protocol_sha256'] == self.plan['protocol_sha256'] and
                all(config[k] == v for k,v in supervisor.expected_configuration().items()),
                'effective configuration mismatch')
            caps = json.loads(self.get('capabilities', '/v1/capabilities')['body'])
            h.require(caps['structured_output']['resources']['terminal_handoff'] ==
                'wait_for_cleanup_with_request_cancellation', 'handoff not advertised')
            self.plain('ordinary-json')
            self.plain('ordinary-sse', stream=True)
            self.strict('strict-before-tool')
            self.tools('project')
            self.incomplete()
            self.strict('after-error')
            self.strict('strict-sse', stream=True)
            self.strict('after-sse')
            self.idle()
            h.require(self.counts['generations'] == 9 and self.counts['negatives'] == 1,
                      'frozen requests not completed')
            for route in ('livez', 'readyz', 'healthz'):
                self.get('final-'+route, '/'+route)
            h.save(self.out/'final-observation.json', observe.sample(self.server, start))
            self.monitor.close(); self.monitor = None
            self.server.terminate()
            h.require(self.server.wait(timeout=45) == 0, 'normal shutdown failed')
            h.require('shutdown complete (0 in-flight remaining)' in
                      (self.out/'server.log').read_text(), 'shutdown did not drain')
            end = self.server.snapshot('payload_exited')
            observe.prior.check_oom(end['resources']['cgroup']['observations']['memory.events']['value'])
            result['status'] = 'PASS_BOUNDED_STRUCTURED_HANDOFF'
        except BaseException as error:
            result.update(status='ENV_ABORT' if isinstance(error, (a.EnvironmentAbort,
                observe.ObservationAbort)) else 'FAILED', error=repr(error))
            raise
        finally:
            try:
                if self.monitor: self.monitor.close()
            finally:
                try:
                    if self.server: self.server.close()
                    a.require_idle_gpu(self.out/'gpu-after.json')
                    result['cleanup_confirmed'] = True
                finally:
                    result.update(counts=self.counts, elapsed_seconds=time.monotonic()-self.started)
                    h.save(self.out/'result.json', result)
