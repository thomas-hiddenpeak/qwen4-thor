"""Frozen real media/text business and error ownership, under one GPU lease."""
import base64
import copy
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from tools.serve_media_failure import stage
from tools.serve_device_memory import verify as prior
from tools.serve_device_memory.correction import capture_response
from tools.serve_lifecycle import observe
from tools.serve_observation_capacity.cpu import command
from isolated_scope import IsolatedScope
from chat_client import RemoteError, read_sse

h, a, ROOT = stage.h, stage.a, stage.ROOT
base = prior.base
FAULT_REQUEST = 'srv02h-failure'
# Name, media, fault kind (device/host), site, device kind, expected exit.
SCENARIOS = (
    ('model', False, None, None, None, 0),
    ('media', True, None, None, None, 0),
    ('media-oom', True, 'device', 'vision.output_allocate', 'oom', 0),
    ('media-copy', True, 'device', 'vision.pixels_copy', 'other', 1),
    ('media-host', True, 'host', 'vision.after_forward', None, 1),
    ('host-early', False, 'host', 'handler.before_diagnostics', None, 0),
    ('host-sse', False, 'host', 'handler.after_header', None, 0),
    ('startup-media', True, 'device', 'startup.vision_load', 'other', 1),
    ('media-free', True, 'device', 'vision.pixels_free', 'other', 1),
)


def environment(kind=None, site=None, error=None):
    env = prior.fault_environment(None, None)
    if kind == 'device':
        env.update(Q4T_SERVE_DEVICE_FAULT=site, Q4T_SERVE_DEVICE_ERROR=error)
    elif kind == 'host':
        env.update(Q4T_SERVE_FAULT=site, Q4T_SERVE_OBSERVE=site)
    if site and not site.startswith('startup.'):
        env['Q4T_SERVE_REQUEST'] = FAULT_REQUEST
    return env


class FixtureRequestContract:
    """Public text schema plus this batch's explicit experimental media subset.

    This validates test fixtures, not an expanded published compatibility claim.
    The actual media bytes are never replaced on the wire.
    """
    def __init__(self, text_validator): self.text = text_validator

    def validate(self, body):
        projected = copy.deepcopy(body)
        for message in projected['messages']:
            content = message.get('content')
            if not isinstance(content, list): continue
            for part in content:
                kind = part.get('type')
                if kind == 'image_url':
                    h.require(set(part) == {'type', 'image_url'} and
                              set(part['image_url']) == {'url'}, 'image fixture shape')
                    urls = [part['image_url']['url']]
                elif kind == 'video_frames':
                    h.require(set(part) == {'type', 'video_frames'} and
                              len(part['video_frames']) == 2, 'video fixture shape')
                    urls = part['video_frames']
                else: continue
                h.require(message['role'] == 'user', 'fixture media role')
                for url in urls:
                    h.require(url.startswith('data:image/png;base64,'), 'media fixture URL')
                    base64.b64decode(url.split(',', 1)[1], validate=True)
                part.clear(); part.update(type='text', text='experimental fixture media')
        self.text.validate(projected)


def media_body(fixtures, *, video=False, invalid=False, stream=False):
    payload = b'invalid-image' if invalid else (Path(fixtures)/'red.png').read_bytes()
    url = 'data:image/png;base64,'+base64.b64encode(payload).decode()
    part = (dict(type='video_frames', video_frames=[url, url]) if video else
            dict(type='image_url', image_url=dict(url=url)))
    body = base.ordinary(stream=stream)
    body['messages'][0]['content'] = [part, dict(type='text',
        text='图像主要是什么颜色？只回复两个字的颜色名称。')]
    return body


def native(plan, folder):
    folder.mkdir()
    a.require_idle_gpu(folder/'gpu-before.json')
    passed = False
    try:
        fixtures = Path(plan['fixtures'])
        ok = command(folder, 'vision', [plan['vision_probe']['path'],
            str(fixtures/'tiny'), str(fixtures/'missing')], timeout=180)
        log = (folder/'vision.stdout').read_text()
        h.require(ok and 'PASS vision_workspace oom=20 copy_failures=4' in log and
                  'PASS vision_weights oom=33 host=3 missing=1 success=1' in log,
                  'real vision ownership contracts')
        ok = command(folder, 'uploads', [plan['upload_probe']['path'], plan['model_dir']], timeout=180)
        log = (folder/'uploads.stdout').read_text()
        h.require(ok and 'PASS uploads=333 ' in log and
                  'semantic_validation=false' in log, 'real visual upload payload equality')
        passed = True
    finally:
        a.require_idle_gpu(folder/'gpu-after.json')
        h.save(folder/'result.json', dict(passed=passed, full_visual_weight_loads=1,
            tiny_weight_failures=dict(oom=33, host=3, missing=1),
            workspace_failures=dict(oom=20, copy=4), physical_oom_or_damage=False))


class Run(prior.Run):
    def __init__(self, plan, output):
        super().__init__(plan, output)
        self.deadline = plan['started']+stage.LIMITS['seconds']-90
        self.schemas['chat'] = FixtureRequestContract(self.schemas['chat'])

    def budget(self, kind=None):
        h.require(time.time() < self.deadline, 'frozen media stage deadline')
        if kind:
            h.require(self.counts[kind] < stage.LIMITS[kind], 'attempt limit: '+kind)
            self.counts[kind] += 1
            h.save(self.out/f'attempt-{kind}-{self.counts[kind]:02d}.json',
                   dict(kind=kind, counts=self.counts.copy(), time=time.time()))

    def http(self, method, path, body=None, *, timeout=5):
        h.require(self.controls < stage.LIMITS['observations'], 'observation limit')
        return super().http(method, path, body, timeout=timeout)

    def failure(self, body, code, *, status=503):
        self.schemas['chat'].validate(body)
        self.budget('negatives'); self.budget('generations')
        h.save(self.folder/'fault-request.json', body)
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=180)
        path = self.folder/'fault-wire.bin'
        receipt = dict(eof=False)
        try:
            conn.request('POST', '/v1/chat/completions', json.dumps(body),
                         {'Content-Type': 'application/json', 'X-Request-Id': body['request_id']})
            response = conn.getresponse()
            receipt.update(status=response.status, headers=dict(response.getheaders()))
            with path.open('xb', buffering=0) as output:
                def sink(part): output.write(part); os.fsync(output.fileno())
                raw = capture_response(response, sink)
            receipt['eof'] = True
        except BaseException as error:
            receipt['read_error'] = repr(error)
            raise
        finally:
            conn.close(); h.save(self.folder/'fault-http.json', receipt)
        h.require(receipt['status'] == status, 'error HTTP status')
        h.require(receipt['headers'].get('X-Request-Id') == body['request_id'], 'error identity')
        stream = status == 200
        values = prior.error_terminal(raw, stream, code)
        for value in values: self.transport.validate(value)
        error = None
        if stream:
            h.require(body['stream'] and receipt['headers']['Content-Type'].startswith(
                'text/event-stream'), 'SSE MIME/mode')
            try: read_sse(io.BytesIO(raw))
            except RemoteError as caught: error = caught
        else: error = RemoteError(status, raw)
        h.require(error is not None and error.code == code and not error.automatic_retry_safe,
                  'same wire must reject unsafe continuation')
        h.save(self.folder/'fault-client.json', dict(code=error.code, status=error.status,
            automatic_retry=False, complete_assistant_turn=False, full_wire_eof=True))
        self.wires.append(dict(file=h.identity(path), envelopes=len(values), success=False))

    def color(self, label, *, video=False):
        call = self.submit(label, media_body(self.plan['fixtures'], video=video, stream=video))
        value = call.finish(); self.check_wire(call)
        h.require(value.finish_reason == 'stop' and
                  value.message['content'].strip().rstrip('。.!！') == '红色',
                  'fixed red image/frame task quality: '+label)
        return value

    def scenario(self, name, media, kind, site, error, expected_exit):
        self.budget('starts')
        self.folder = self.out/name; self.folder.mkdir()
        self.server = None
        a.require_idle_gpu(self.folder/'gpu-before.json')
        binary = self.plan['binary' if kind is None else 'validation_binary']
        argv = [binary['path'], 'serve', '--model-dir', self.plan['model_dir'],
                '--host', '127.0.0.1', '--port', str(self.port), '--max-prefill', '2048',
                '--max-len', '8192', '--max-seq', '1', '--max-inflight-requests', '8',
                '--max-queued-requests', '2']
        if media: argv += ['--allow-media', '--no-mtp']
        h.save(self.folder/'command.json', dict(argv=argv, binary=binary, kind=kind,
            site=site, error=error, expected_exit=expected_exit, media=media))
        passed = False
        try:
            self.server = IsolatedScope(argv, cwd=ROOT, env=environment(kind, site, error),
                log_path=self.folder/'server.log', unit=f'q4t-media-{name}-{os.getpid()}.scope',
                memory_max=None, evidence=self.folder/'scope')
            start = h.read(self.folder/'scope/identity.json')['start_ticks']
            if name == 'startup-media':
                self.budget('negatives')
                rc = self.server.wait(timeout=240)
                log = (self.folder/'server.log').read_text()
                h.require(rc == 1 and 'serving on port' not in log and
                    'requested vision load failed' in log and 'text-only mode' not in log,
                    'requested visual startup silently degraded')
            else:
                self.monitor = observe.Monitor(self.server, start, self.folder)
                end = time.monotonic()+240
                while 'serving on port' not in (self.folder/'server.log').read_text():
                    self.budget(); self.monitor.check()
                    h.require(self.server.poll() is None and time.monotonic() < end,
                              'startup failed: '+name)
                    time.sleep(.25)
                for path, label in (('/v1/capabilities', 'capabilities'), ('/v1/config', 'configuration')):
                    response = self.http('GET', path)
                    h.save(self.folder/(label+'.json'), response)
                    h.require(response['status'] == 200, 'discovery')
                    parsed = json.loads(response['raw'])
                    if label == 'configuration':
                        self.config_schema.validate(parsed)
                        h.require(parsed['protocol_source_sha256'] == self.plan['protocol_sha256'],
                                  'source identity')
                    else:
                        h.require(parsed['errors']['host_memory']['http_status'] == 503 and
                            parsed['media_failures']['requested_load_failure'] == 'startup_rejected' and
                            parsed['runtime']['media_enabled'] == media, 'failure capability profile')
                self.drain('before-business')
                if name == 'model': self.production()
                elif name == 'media':
                    self.color('image'); self.color('video', video=True)
                    self.failure(media_body(self.plan['fixtures'], invalid=True),
                                 'invalid_request', status=400)
                    rows = self.drain('media-complete')
                    h.require(rows['q4t_requests_total'] == 3 and
                              rows['q4t_requests_success_total'] == 2 and
                              rows['q4t_requests_error_total'] == 1, 'media accounting')
                else:
                    if expected_exit:
                        h.save(self.folder/'before-fatal-observation.json', observe.sample(self.server, start))
                        self.stop_monitor()
                    body = media_body(self.plan['fixtures']) if media else base.ordinary(stream=name == 'host-sse')
                    body['request_id'] = FAULT_REQUEST
                    code = ('host_memory_exhausted' if kind == 'host' else
                            'device_memory_exhausted' if error == 'oom' else 'device_memory_failure')
                    self.failure(body, code, status=200 if name == 'host-sse' else 503)
                    if expected_exit:
                        h.require(self.server.wait(timeout=45) == 1, 'fatal media failure did not stop')
                    else:
                        if media: self.color('recovered')
                        else: self.success('recovered', base.ordinary())
                        rows = self.drain('recovered')
                        h.require(rows['q4t_requests_total'] == 2 and
                                  rows['q4t_requests_success_total'] == 1 and
                                  rows['q4t_requests_error_total'] == 1 and
                                  rows['q4t_requests_aborted_total'] == 0, 'recovery accounting')
                        h.require(h.read(self.folder/'scope/identity.json')['start_ticks'] == start and
                                  self.server.poll() is None, 'recovery changed process')
                if not expected_exit:
                    self.health('final')
                    h.save(self.folder/'final-observation.json', observe.sample(self.server, start))
                self.stop_monitor()
            if self.server.poll() is None: self.server.terminate()
            rc = self.server.wait(timeout=45)
            log = (self.folder/'server.log').read_text()
            h.require(rc == expected_exit, 'unexpected exit: '+name)
            if kind == 'device':
                h.require(log.count('[serve-test-device]') == 1 and f'site={site} ' in log,
                          'device injection binding')
            elif kind == 'host':
                h.require(log.count(f'[serve-test] observed={site}') == 1, 'host fault binding')
            else:
                h.require('[serve-test' not in log, 'production hook active')
            h.require('[cleanup-fatal]' not in log, 'unexpected unknown cleanup')
            if not expected_exit:
                h.require('shutdown complete (0 in-flight remaining)' in log, 'normal drain')
            snap = self.server.snapshot('payload_exited')
            observe.prior.check_oom(snap['resources']['cgroup']['observations']['memory.events']['value'])
            h.save(self.folder/'result.json', dict(passed=True, returncode=rc, site=site,
                simulated_failure=kind is not None, physical_oom_or_damage=False))
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
            result.update(status='PASS_BOUNDED_MEDIA_FAILURE', performance_acceptance=False,
                no_retry=True, physical_oom_or_damage=False, broad_multimodal_quality=False)
        except BaseException as error:
            result.update(status='ENV_ABORT' if isinstance(error, (a.EnvironmentAbort,
                observe.ObservationAbort)) else 'FAILED', error=repr(error))
            raise
        finally:
            result.update(counts=self.counts, control_requests=self.controls,
                          elapsed_seconds=time.monotonic()-self.started)
            h.save(self.out/'result.json', result)
