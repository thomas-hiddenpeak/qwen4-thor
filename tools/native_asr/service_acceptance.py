"""Native ASR managed TLS, scoped uploads and exact retained/version recovery.

Reuses the owned-tree lifecycle, strict budgets and cleanup verifier. Actual
transcription wire is required; CPU fixtures never qualify a service payload.
"""
import base64
import copy
import http.client
import json
import os
from pathlib import Path
import runpy
import socket
import ssl
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BASE = runpy.run_path(str(ROOT/'tools/serve_responses/web_service_acceptance.py'))
GLOBALS = BASE['main'].__globals__
VERSION, MANAGED, LEGACY = BASE['VERSION'], BASE['MANAGED'], BASE['LEGACY']
require, save = BASE['require'], BASE['save']
LIMITS = dict(tool_retained=dict(starts=1, chats=3, probes=0, controls=1500, seconds=300),
              tool_version=dict(starts=2, chats=6, probes=1, controls=2500, seconds=600),
              tls=dict(starts=1, chats=7, probes=0, controls=2000, seconds=600))
WORK = ROOT/'.q4t-work/serve-audio-native-20261117'


def direct():
    # Import only after production namespaces have been pinned for an actual
    # managed run. The reference driver is never part of the production pack.
    from tools.native_asr import transcription_acceptance
    return transcription_acceptance


def requests():
    return direct().requests()


def prepare(args):
    bundle, _, _, _, _, engine = LEGACY['load_modules']()
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit reviewed source before freezing')
    out = args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'), 'private output required')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    pack = args.package.resolve(strict=True)
    digest = engine.sha(pack/'manifest.json'); manifest = bundle.verify(pack, digest)
    policy = engine.read(pack/'gateway-policy.json')
    require(manifest['ingress_mode'] == 'native' and
        bool(manifest['tls']) == (args.scenario == 'tls') and
        policy['listen_host'] == '127.0.0.1' and
        manifest['recovery_profiles']['identity'].get('native_asr'),
        'exact native ASR qualified payload/TLS mode required')
    names = subprocess.check_output(['git', 'ls-files', 'tools/serve_recovery',
        'tools/serve_host', 'tools/serve_service', 'tools/serve_ops', 'tools/serve_gateway',
        'tools/evalscope', 'tools/examples', 'tools/release', 'tools/serve_responses',
        'tools/native_asr', 'src/service', 'include/q4t/service'], cwd=ROOT, text=True).splitlines()
    plan = dict(version=1, scenario=args.scenario, limits=LIMITS[args.scenario],
        package=dict(package=str(pack), manifest_sha256=digest), output=str(out),
        sources={n:engine.sha(ROOT/n) for n in sorted(set(names)|bundle.NATIVE_SOURCES)},
        protected=[dict(path=str(ROOT/n), sha256=engine.sha(ROOT/n)) for n in
            ('build/q4t', 'build/q4t.release.json', 'build/service/manifest.json',
             'build/service.release.json', 'build/service-native-state/ledger.json')],
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(),
        command=[sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                 'run', '--plan', str(out/'plan.json')], requests=requests(),
        default_replacement=False, automatic_request_replay=False,
        planned=dict(starts=LIMITS[args.scenario]['starts'],
            self_checks=LIMITS[args.scenario]['starts'],
            counted_post_requests=LIMITS[args.scenario]['chats']), scope=__doc__)
    reference = engine.read(WORK/'service-http-05/plan.json')
    plan['clips'] = reference['clips']
    plan['sdk'] = reference['sdk']
    plan['python_sdk'] = direct().python_identity()
    for row in plan['clips'].values():
        require(engine.sha(row['path']) == row['sha256'], 'real audio changed')
        plan['sources'][row['path']] = row['sha256']
    plan['sources'].update(plan['python_sdk']['files'])
    plan['sources'][plan['python_sdk']['python']] = plan['python_sdk']['executable_sha256']
    plan['sources'].update({str(Path(plan['sdk']['sdk_directory'])/name):digest
                            for name, digest in plan['sdk']['files'].items()})
    plan['sources'][plan['sdk']['node']['path']] = plan['sdk']['node']['sha256']
    require(all(engine.sha(ROOT/name) == digest for name, digest in plan['sources'].items()),
            'frozen validation source changed')
    if args.scenario == 'tls':
        # This bounded fixture uses its private self-signed certificate as CA.
        # Hostname and certificate verification stay enabled in both SDKs.
        cert = Path(manifest['tls_configuration']['certificate']).resolve(strict=True)
        require(cert.is_relative_to(WORK), 'dedicated private TLS fixture required')
        plan['tls_inputs'] = dict(ca=dict(path=str(cert), sha256=engine.sha(cert)))
        for key in ('certificate', 'key'):
            path = Path(manifest['tls_configuration'][key]).resolve(strict=True)
            require(path.is_relative_to(WORK), 'private TLS material required')
            plan['tls_inputs'][key] = dict(path=str(path), sha256=engine.sha(path))
        backend = engine.read(pack/'engine/client-policy.json')
        limits = {c['id']:c for c in backend['clients']}
        require(backend['version'] == 4 and
            limits['alpha']['max_file_body_bytes'] >= 3*1024**2 and
            limits['beta']['max_file_body_bytes'] == 65536,
            'distinct file upload limits required')
    if args.scenario == 'tool_version':
        from tools.serve_recovery import version_contract
        require(args.retained and args.release and args.decision,
                'finalized exact retained acceptance required')
        retained = args.retained.resolve(strict=True)
        acceptance = {name:dict(path=str(path.resolve(strict=True)), sha256=engine.sha(path))
            for name, path in [('release', args.release), ('decision', args.decision)]}
        plan['registration'] = version_contract.prepare(plan['package'], plan['package'],
            dict(package=str(retained), manifest_sha256=engine.sha(retained/'manifest.json')),
            str(uuid.uuid4()), fallback_acceptance=acceptance)
        helper = out/'counted-cuda-probe'
        helper.write_text('#!'+sys.executable+'\nimport runpy,sys\nsys.argv='+repr([
            str(Path(__file__).resolve()), 'probe', '--plan', str(out/'plan.json')])+
            '\nrunpy.run_path(sys.argv[0],run_name="__main__")\n')
        helper.chmod(0o500)
        plan['probe_wrapper'] = dict(path=str(helper), sha256=engine.sha(helper))
    save(out/'plan.json', plan)
    print(json.dumps(dict(plan=str(out/'plan.json'), limits=plan['limits'])))


def stream_value(raw):
    value = direct()
    events = [json.loads(line[6:]) for line in raw.decode().splitlines()
              if line.startswith('data: {')]
    require(events and events[-1]['type'] == 'transcript.text.done' and
        all(e['type'] == 'transcript.text.delta' for e in events[:-1]) and
        b'[DONE]' not in raw, 'wrong transcription event terminal')
    final = events[-1]
    require(''.join(e['delta'] for e in events[:-1]) == final['text'] and
        [p for e in events[:-1] for p in e.get('logprobs', [])] == final['logprobs'],
        'delta/final text or probabilities differ')
    value.usage(final)
    return final


def check_business(out):
    read = lambda n: json.loads((out/n).read_text())
    plan = json.loads((out.parent/'plan.json').read_text())
    tls, version = plan['scenario'] == 'tls', plan['scenario'] == 'tool_version'
    results = {}
    for label in (['python-zh-json', 'chunked-zh'] if tls else
                  ['primary-asr', 'fallback-asr'] if version else ['primary-asr']):
        raw = (out/(label+'.wire')).read_bytes()
        answer = stream_value(raw) if label == 'fallback-asr' else json.loads(raw)
        direct().usage(answer);results[label] = direct().quality(answer['text'], 'zh')
    if tls:
        results['js-en-stream'] = direct().quality(
            stream_value((out/'js-en-stream.wire').read_bytes())['text'], 'en')
        require(read('beta-file-limit-http.json')['status'] == 413 and
            read('unauthorized-http.json')['status'] == 401, 'scope/upload guards failed')
        for label in ('python-zh-json', 'js-en-stream'):
            sdk = read(label+'.sdk.json')
            require(sdk['ok'] and len(sdk['exchanges']) == 1, 'SDK retried or failed')
            if label == 'python-zh-json':
                require(sdk['version'] == '2.8.1' and
                    sdk['result']['text'] == read(label+'.wire')['text'], 'Python SDK text differs')
                direct().usage(sdk['result'], sdk=True)
            else:
                events = [json.loads(line[6:]) for line in (out/(label+'.wire')).read_text().splitlines()
                          if line.startswith('data: {')]
                require(sdk['version'] == '7.47.0' and sdk['result']['events'] == events,
                        'JS SDK event projection differs')
        cancelled = read('cancel-observation.json')
        require(cancelled['after']['q4t_requests_aborted_total'] ==
            cancelled['before']['q4t_requests_aborted_total']+1,
            'native disconnect failed to cancel actual transcription')
    answer = read('core-after.wire')['choices'][0]
    require(answer['finish_reason'] == 'stop' and
        answer['message']['content'].strip() == '42', 'core failed after ASR')
    config = read('final-config.wire')
    health = config['native_asr_health']
    expected = json.loads((Path(plan['package']['package'])/'engine/manifest.json').read_text())
    require(health['state'] == 'ready' and health['compute_failures'] == 0 and
        health['core_readiness_dependency'] is False and
        config['native_asr_limits'] == expected['settings']['native_asr']['limits'],
        'ASR health or configured budget lost')
    continuity = read('core-continuity.json')
    before, after = continuity['before'], continuity['after']
    fields = ('model_pid', 'model_start_ticks', 'controller_pid', 'controller_start_ticks')
    if version:
        require(all(after[role+'_generation'] > before[role+'_generation'] and
                    after[role+'_pid'] != before[role+'_pid']
                    for role in ('controller', 'model', 'gateway')) and
            after['guardian_pid'] == before['guardian_pid'] and
            after['version_recovery']['selected'] == 'fallback' and
            after['version_recovery']['probe_count'] == 1, 'whole-payload recovery missing')
        state = read('profile-continuity.json')
        require(state['before'] == state['after'], 'recovery erased original profile budget')
        require(read('stored-before.wire')['choices'] == read('stored-after.wire')['choices'],
                'stored Chat lost on recovery')
    else:
        require(all(before[k] == after[k] for k in fields), 'healthy core restarted')
        if tls:
            require(after['gateway_generation'] == before['gateway_generation']+1 and
                after['gateway_pid'] != before['gateway_pid'], 'native gateway did not recover')
    return dict(results=results, native_tls=tls, version_recovery=version,
                health=health, actual_core_text='42', default_replaced=False)


def business_record(version):
    return dict(status='PASS', actual_transcription=True, original_budget=True,
        version_recovery=version, default_replaced=False, automatic_request_or_tool_replay=False)


class Stage(BASE['Stage']):
    def initialize_state(self):
        return VERSION['Stage'].initialize_state(self) if self.plan['scenario'] == 'tool_version' \
            else LEGACY['Stage'].initialize_state(self)

    def connection(self):
        if self.plan['scenario'] == 'tls':
            context = ssl.create_default_context(cafile=self.plan['tls_inputs']['ca']['path'])
            return http.client.HTTPSConnection('127.0.0.1', self.port, timeout=120, context=context)
        return http.client.HTTPConnection('127.0.0.1', self.port, timeout=120)

    def request(self, label, body=None, *, client='alpha', path='/v1/models', method=None,
                expected=200, model_work=False, raw_result=False,
                content_type='application/json', chunked=False, cancel=False):
        time.sleep(.22)
        self.budget.reserve('chats' if model_work else 'controls', label)
        raw = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        if body is not None and not isinstance(body, bytes):save(self.output/(label+'-request.json'), body)
        connection = self.connection()
        try:
            headers = {'Authorization':'Bearer '+self.keys[client], 'Content-Type':content_type}
            wire_body = (raw[i:i+4096] for i in range(0, len(raw), 4096)) if chunked else raw
            connection.request(method or ('GET' if body is None else 'POST'), path,
                wire_body, headers, encode_chunked=chunked)
            response = connection.getresponse()
            if cancel:
                require(response.status == 200, 'cancel request did not stream')
                wire = b''
                while len(wire) < 1024**2:
                    line = response.readline(65537)
                    require(line and len(line) <= 65536, 'stream ended before cancellation')
                    wire += line
                    if line.startswith(b'data: {'):
                        event = json.loads(line[6:])
                        if event.get('type') == 'transcript.text.delta' and event['delta']:break
                else:raise RuntimeError('cancellation byte budget')
                if connection.sock:connection.sock.shutdown(socket.SHUT_RDWR)
                response.close()
            else:wire = response.read(4*1024**2+1)
            require(len(wire) <= 4*1024**2, 'response byte budget')
            (self.output/(label+'.wire')).write_bytes(wire)
            save(self.output/(label+'-http.json'), dict(status=response.status,
                headers=dict(response.getheaders()), chunked=chunked, cancel=cancel))
            require(response.status == expected, 'unexpected HTTP status: '+label)
            return wire if raw_result or cancel else json.loads(wire)
        finally:connection.close()

    def audio(self, label, *, stream=False, client='alpha', expected=200, chunked=False,
              cancel=False, language='zh'):
        boundary = 'q4t-native-asr-service'
        fields = [('model', 'qwen3-asr-0.6b'), ('include[]', 'logprobs')]
        if stream:fields.append(('stream', 'true'))
        parts = [(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                  f'{value}\r\n').encode() for key, value in fields]
        audio = Path(self.plan['clips'][language]['path']).read_bytes()
        parts += [(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                   'filename="voice.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode(),
                  audio, f'\r\n--{boundary}--\r\n'.encode()]
        return self.request(label, b''.join(parts), client=client,
            path='/v1/audio/transcriptions', model_work=True, expected=expected,
            content_type='multipart/form-data; boundary='+boundary,
            raw_result=stream, chunked=chunked, cancel=cancel)

    def sdk(self, label, language, python=False):
        self.budget.reserve('chats', label)
        config = dict(base_url=f'https://127.0.0.1:{self.port}/v1',
            api_key=self.keys['alpha'], ca=self.plan['tls_inputs']['ca']['path'],
            audio=self.plan['clips'][language]['path'], filename=language+'.wav',
            sdk_directory=self.plan['sdk']['sdk_directory'],
            request=self.plan['requests']['zh' if python else 'en'])
        save(self.output/(label+'-request.json'), {k:v for k,v in config.items() if k != 'api_key'})
        command = [self.plan['python_sdk']['python'], '-I', '-B',
                   str(ROOT/'tools/native_asr/transcription_sdk.py')] if python else \
                  [self.plan['sdk']['node']['path'], str(ROOT/'tools/native_asr/transcription_sdk.mjs')]
        env = {k:v for k,v in os.environ.items() if k not in
            ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD', 'HTTP_PROXY', 'HTTPS_PROXY',
             'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'NODE_USE_ENV_PROXY')}
        env.update(PYTHONDONTWRITEBYTECODE='1', NODE_EXTRA_CA_CERTS=config['ca'])
        run = subprocess.run(command, input=json.dumps(config), capture_output=True,
                             text=True, env=env, timeout=190)
        (self.output/(label+'.stderr')).write_text(run.stderr)
        require(run.stdout, 'SDK returned no evidence')
        value = json.loads(run.stdout)
        (self.output/(label+'.wire')).write_bytes(base64.b64decode(value.pop('wire_base64'), validate=True))
        save(self.output/(label+'.sdk.json'), value)
        require(run.returncode == 0 and value['ok'] and len(value['exchanges']) == 1,
                'actual native TLS SDK failed: '+label)

    def drained(self, label):
        from tools.serve_ops import contracts, transport
        end = time.monotonic()+5
        observations = []
        while time.monotonic() < end:
            self.budget.reserve('controls', label)
            response = transport.get(self.policy['upstream_operations_port'], '/metrics', timeout=1)
            require(response['http_status'] == 200, 'metrics unavailable')
            values, _ = contracts.parse_metrics(response['body'], version=5)
            observations.append(dict(response=response, values=values))
            save(self.output/(label+'.json'), observations)
            if (values['q4t_seq_slots_free'] == values['q4t_seq_slots_total'] == 1 and
                all(values[k] == 0 for k in ('q4t_active_chats',
                    'q4t_structured_cleanup_pending', 'q4t_structured_workers_active',
                    'q4t_structured_active', 'q4t_structured_preparing')) and
                values['q4t_structured_workers_started_total'] ==
                    values['q4t_structured_workers_reaped_total']):
                return values
            time.sleep(.05)
        raise TimeoutError('owned ASR request did not drain')

    def exercise(self):
        version, tls = self.plan['scenario'] == 'tool_version', self.plan['scenario'] == 'tls'
        first = self.version_ready(1, 'primary') if version else self.ready(1, 'primary')
        self.request('capabilities', path='/v1/capabilities')
        if tls:
            self.request('unauthorized', client='invalid', expected=401)
            self.sdk('python-zh-json', 'zh', python=True)
            self.kill(first, 'gateway', 'native-asr-gateway-exit')
            self.wait(lambda s:s['business_ready'] and
                      s['gateway_generation'] == first['gateway_generation']+1)
            self.sdk('js-en-stream', 'en')
            self.audio('beta-file-limit', client='beta', expected=413)
            self.audio('chunked-zh', chunked=True)
            before = self.drained('before-cancel')
            self.audio('cancel-stream', stream=True, cancel=True, language='en')
            after = self.drained('after-cancel')
            save(self.output/'cancel-observation.json', dict(before=before, after=after))
        else:self.audio('primary-asr')
        if version:
            self.request('stored-before', dict(self.plan['requests']['chat'], store=True),
                         path='/v1/chat/completions', model_work=True)
            fields = ('history', 'pending', 'selected', 'not_before_ns', 'light_since_ns')
            before = self.family('profiles')
            self.fatal_gateway(first)
            self.version_ready(2, 'fallback')
            self.audio('fallback-asr', stream=True)
            stored = json.loads((self.output/'stored-before.wire').read_text())
            self.request('stored-after', path='/v1/chat/completions/'+stored['id'])
            after = self.family('profiles')
            save(self.output/'profile-continuity.json', dict(
                before={k:before[k] for k in fields}, after={k:after[k] for k in fields}))
        self.request('core-after', self.plan['requests']['chat'], path='/v1/chat/completions',
                     client='beta' if tls else 'alpha', model_work=True)
        self.request('final-config', path='/v1/config')
        after = self.version_ready(2, 'fallback') if version else self.ready(1, 'primary')
        save(self.output/'core-continuity.json', dict(before=first, after=after))
        save(self.output/'media-usage.json', check_business(self.output))
        save(self.output/'business-final.json', business_record(version))


GLOBALS.update(__file__=__file__, LIMITS=LIMITS, requests=requests, prepare=prepare,
               Stage=Stage, check_business=check_business, business_record=business_record,
               ACCEPTANCE_PREFIX='NATIVE_ASR_SERVICE_')
for function in (VERSION['launcher'], MANAGED['launcher']):
    function.__globals__['__file__'] = __file__

if __name__ == '__main__':
    BASE['main']()
