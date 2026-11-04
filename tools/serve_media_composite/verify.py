"""Combined media ownership, admission and cancellation on one exclusive lease."""
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

from tools.serve_media_failure import verify as old
from tools.serve_media_composite import fixtures, stage

h, a, ROOT = stage.h, stage.a, stage.ROOT
SCENARIOS = (*old.SCENARIOS, ('media-admission', True, 'admission', None, None, 0),
             ('media-cancel', True, 'cancel', None, None, 0))


def environment(name, gate=None):
    env = old.environment()
    if name == 'media-admission':
        env.update(Q4T_SERVE_DEVICE_FAULT='vision.pixels_allocate',
                   Q4T_SERVE_DEVICE_ERROR='oom', Q4T_SERVE_REQUEST=fixtures.FAULT_REQUEST)
    elif name == 'media-cancel':
        h.require(gate is not None, 'cancel gate missing')
        env.update(Q4T_SERVE_REQUEST=fixtures.CANCEL_REQUEST,
                   Q4T_SERVE_PAUSE='vision.after_forward',
                   Q4T_SERVE_OBSERVE='vision.after_forward', Q4T_SERVE_GATE=str(gate))
    else:
        raise ValueError('unknown added scenario')
    return env


def capture_failure(folder, label, port, body, status, code, param, transport):
    """Capture through EOF before parsing error; no retry or early SSE return."""
    h.save(folder/(label+'-request.json'), body)
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=180)
    path = folder/(label+'-wire.bin')
    receipt = dict(eof=False)
    try:
        conn.request('POST', '/v1/chat/completions', json.dumps(body),
                     {'Content-Type': 'application/json', 'X-Request-Id': body['request_id']})
        response = conn.getresponse()
        receipt.update(status=response.status, headers=dict(response.getheaders()))
        with path.open('xb', buffering=0) as output:
            def sink(part): output.write(part); os.fsync(output.fileno())
            raw = old.capture_response(response, sink)
        receipt['eof'] = True
    except BaseException as error:
        receipt['read_error'] = repr(error)
        raise
    finally:
        conn.close(); h.save(folder/(label+'-http.json'), receipt)
    h.require(receipt['status'] == status and
              receipt['headers'].get('X-Request-Id') == body['request_id'], 'HTTP status/identity')
    values = old.prior.error_terminal(raw, status == 200, code)
    for value in values: transport.validate(value)
    if param is not None:
        h.require(values[-1]['error']['param'] == param, 'error location: '+label)
    error = None
    if status == 200:
        h.require(body['stream'] and receipt['headers']['Content-Type'].startswith(
            'text/event-stream'), 'SSE MIME')
        try: old.read_sse(io.BytesIO(raw))
        except old.RemoteError as caught: error = caught
    else:
        error = old.RemoteError(status, raw)
    h.require(error is not None and error.code == code and not error.automatic_retry_safe,
              'client must reject unsafe continuation')
    h.save(folder/(label+'-client.json'), dict(code=error.code, status=error.status,
        automatic_retry=False, complete_assistant_turn=False, full_wire_eof=True))
    return dict(file=h.identity(path), envelopes=len(values), success=False)


class Run(old.Run):
    def __init__(self, plan, output):
        super().__init__(plan, output)
        self.deadline = plan['started']+stage.LIMITS['seconds']-90

    def budget(self, kind=None):
        h.require(time.time() < self.deadline, 'composite stage deadline')
        if kind:
            h.require(self.counts[kind] < stage.LIMITS[kind], 'attempt limit: '+kind)
            self.counts[kind] += 1
            h.save(self.out/f'attempt-{kind}-{self.counts[kind]:02d}.json',
                   dict(kind=kind, counts=self.counts.copy(), time=time.time()))

    def http(self, method, path, body=None, *, timeout=5):
        h.require(self.controls < stage.LIMITS['observations'], 'observation limit')
        self.controls += 1
        response = old.base.Run.http(self, method, path, body, timeout=timeout)
        if method == 'GET' and path in ('/v1/config', '/v1/capabilities') and response['status'] == 200:
            value = json.loads(response['raw'])
            runtime = value['runtime']
            policy = runtime['media_limits'] if path.endswith('config') else value['media']
            expected = self.plan['actual_attention_shared_bytes'] if runtime['media_enabled'] else None
            h.require(policy['attention_shared_bytes'] == expected and
                      policy['decoder_owned_bytes'] == fixtures.DECODER_LIMIT and
                      not policy['whole_server_ram_limit'], 'actual media policy discovery')
        return response

    def failure(self, body, code, *, status=503):
        # The unchanged old malformed payload now fails at the header boundary.
        param = None
        if self.folder.name == 'media':
            h.require(code == 'invalid_request' and status == 400, 'old media fixture changed')
            code, param = 'media_format_unsupported', 'messages[0].content[0].image_url'
        self.schemas['chat'].validate(body)
        self.budget('negatives'); self.budget('generations')
        self.wires.append(capture_failure(self.folder, 'fault', self.port, body,
                                           status, code, param, self.transport))

    def admission(self):
        rows = h.read(Path(self.plan['fixtures'])/'cases.json')
        h.require([row['name'] for row in rows] == list(fixtures.NAMES), 'fixed cases changed')
        for row in rows:
            self.budget('negatives'); self.budget('generations')
            self.wires.append(capture_failure(self.folder, row['name'], self.port,
                row['body'], row['status'], row['code'], row['param'], self.transport))
            self.drain('after-'+row['name'])
            h.require('[serve-test-device]' not in (self.folder/'server.log').read_text(),
                      'rejected request reached first visual device allocation')
        body = old.media_body(self.plan['fixtures'])
        body['request_id'] = fixtures.FAULT_REQUEST
        self.failure(body, 'device_memory_exhausted')
        log = (self.folder/'server.log').read_text()
        h.require(log.count('[serve-test-device]') == 1 and
                  'site=vision.pixels_allocate ' in log, 'armed allocation sentinel was not reached')
        self.color('after-rejects')
        metrics = self.drain('admission-recovered')
        h.require(metrics['q4t_requests_total'] == 13 and
                  metrics['q4t_requests_success_total'] == 1 and
                  metrics['q4t_requests_error_total'] == 12 and
                  metrics['q4t_requests_aborted_total'] == 0, 'admission accounting')

    def cancellation(self, gate):
        body = old.media_body(self.plan['fixtures'], stream=True)
        body['request_id'] = fixtures.CANCEL_REQUEST
        self.schemas['chat'].validate(body)
        self.budget('negatives'); self.budget('generations')
        pending = SimpleNamespace(error=None, result=None, thread=None)
        def run():
            try:
                pending.result = capture_failure(self.folder, 'cancelled', self.port,
                    body, 200, 'explicit_cancel', None, self.transport)
            except BaseException as error:
                pending.error = error
        pending.thread = threading.Thread(target=run, name='media-cancel-wire', daemon=True)
        self.calls.append(pending)
        pending.thread.start()
        entered, release = Path(str(gate)+'.entered'), Path(str(gate)+'.release')
        try:
            end = time.monotonic()+30
            while not entered.exists():
                self.budget(); self.monitor.check()
                h.require(pending.thread.is_alive() and self.server.poll() is None and
                          time.monotonic() < end, 'visual completion gate not reached')
                time.sleep(.01)
            response = self.http('POST', '/v1/requests/cancel',
                {key: body[key] for key in ('request_id', 'cancel_token')}, timeout=2)
            h.save(self.folder/'cancel-control.json', response)
            h.require(response['status'] == 202, 'media cancellation rejected')
        finally:
            release.touch(exist_ok=False)
        pending.thread.join(timeout=10)
        h.require(not pending.thread.is_alive(), 'cancelled response failed to complete')
        if pending.error: raise pending.error
        h.require(pending.result is not None, 'missing cancellation wire')
        self.wires.append(pending.result)
        self.drain('cancelled-drained')
        self.color('after-cancel')
        metrics = self.drain('cancel-recovered')
        h.require(metrics['q4t_requests_total'] == 2 and
                  metrics['q4t_requests_success_total'] == 1 and
                  metrics['q4t_requests_error_total'] == 0 and
                  metrics['q4t_requests_aborted_total'] == 1, 'cancel accounting')

    def scenario(self, name, media, kind, site, error, expected_exit):
        if kind not in ('admission', 'cancel'):
            return super().scenario(name, media, kind, site, error, expected_exit)
        self.budget('starts')
        self.folder = self.out/name; self.folder.mkdir()
        self.server = None
        a.require_idle_gpu(self.folder/'gpu-before.json')
        gate = self.folder/'after-vision'
        binary = self.plan['validation_binary']
        argv = [binary['path'], 'serve', '--model-dir', self.plan['model_dir'],
                '--host', '127.0.0.1', '--port', str(self.port), '--max-prefill', '2048',
                '--max-len', '8192', '--max-seq', '1', '--max-inflight-requests', '8',
                '--max-queued-requests', '2', '--allow-media', '--no-mtp']
        h.save(self.folder/'command.json', dict(argv=argv, binary=binary, kind=kind,
            gate=str(gate) if kind == 'cancel' else None, expected_exit=0,
            physical_oom_or_damage=False))
        passed = False
        try:
            self.server = old.IsolatedScope(argv, cwd=ROOT, env=environment(name, gate),
                log_path=self.folder/'server.log', unit=f'q4t-media-{name}-{os.getpid()}.scope',
                memory_max=None, evidence=self.folder/'scope')
            start = h.read(self.folder/'scope/identity.json')['start_ticks']
            self.monitor = old.observe.Monitor(self.server, start, self.folder)
            end = time.monotonic()+240
            while 'serving on port' not in (self.folder/'server.log').read_text():
                self.budget(); self.monitor.check()
                h.require(self.server.poll() is None and time.monotonic() < end, 'media startup failed')
                time.sleep(.25)
            for path, label in (('/v1/capabilities', 'capabilities'), ('/v1/config', 'configuration')):
                value = self.http('GET', path)
                h.save(self.folder/(label+'.json'), value)
                h.require(value['status'] == 200, 'discovery failed')
                parsed = json.loads(value['raw'])
                if label == 'configuration':
                    self.config_schema.validate(parsed)
                    h.require(parsed['protocol_source_sha256'] == self.plan['protocol_sha256'] and
                              parsed['runtime']['media_enabled'], 'effective identity/mode')
            self.drain('before-business')
            if kind == 'admission': self.admission()
            else: self.cancellation(gate)
            h.require(h.read(self.folder/'scope/identity.json')['start_ticks'] == start and
                      self.server.poll() is None, 'recovery changed process')
            self.health('final')
            h.save(self.folder/'final-observation.json', old.observe.sample(self.server, start))
            self.stop_monitor()
            self.server.terminate()
            rc = self.server.wait(timeout=45)
            log = (self.folder/'server.log').read_text()
            h.require(rc == 0 and 'shutdown complete (0 in-flight remaining)' in log and
                      '[cleanup-fatal]' not in log, 'media shutdown')
            if kind == 'cancel':
                h.require(log.count('[serve-test] observed=vision.after_forward') == 1 and
                          '[serve-test-device]' not in log, 'cancel hook binding')
            snapshot = self.server.snapshot('payload_exited')
            old.observe.prior.check_oom(snapshot['resources']['cgroup']['observations']['memory.events']['value'])
            h.save(self.folder/'result.json', dict(passed=True, returncode=rc,
                same_pid_recovery=True, cooperative_cancellation_only=kind == 'cancel'))
            passed = True
        finally:
            try: self.stop_monitor()
            finally:
                if self.server:
                    try:
                        if self.server.poll() is None:
                            self.server.terminate()
                            try: self.server.wait(timeout=45)
                            except subprocess.TimeoutExpired:
                                self.server.kill(); self.server.wait(timeout=10)
                        if not passed:
                            h.save(self.folder/'cleanup.json', dict(returncode=self.server.poll(), accepted=False))
                    finally: self.server.close()
                for call in self.calls:
                    call.thread.join(timeout=10)
                    h.require(not call.thread.is_alive(), 'owned client still running')
                a.require_idle_gpu(self.folder/'gpu-after.json')

    def run(self):
        result = dict(status='FAILED')
        try:
            with socket.socket() as sock: sock.bind(('127.0.0.1', self.port))
            for row in SCENARIOS: self.scenario(*row)
            h.require(self.counts == stage.PLANNED, 'frozen attempt count changed')
            h.save(self.out/'wire-schema-result.json', dict(passed=True, records=self.wires))
            result.update(status='PASS_BOUNDED_MEDIA_COMPOSITE', performance_acceptance=False,
                no_retry=True, physical_oom_or_damage=False, broad_multimodal_quality=False)
        except BaseException as error:
            result.update(status='ENV_ABORT' if isinstance(error, (a.EnvironmentAbort,
                old.observe.ObservationAbort)) else 'FAILED', error=repr(error))
            raise
        finally:
            result.update(counts=self.counts, control_requests=self.controls,
                          elapsed_seconds=time.monotonic()-self.started)
            h.save(self.out/'result.json', result)
