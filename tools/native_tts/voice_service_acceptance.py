"""One owned server: real sample registration, scoped Base speech and revoke."""
import argparse
import base64
import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import time
import traceback
import wave

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_tts import chat_acceptance as chat
from tools.native_tts import http_acceptance as speech
from tools.native_tts.acceptance import defaults
from tools.native_tts.utterance_acceptance import normalize
from tools.serve_service.package import read, require, save, sha

lifecycle = speech.lifecycle
WORK = ROOT/'.q4t-work/serve-custom-voices-20261118'
BASE = 'qwen3-tts-12hz-1.7b-base'
CONSENTS = '/v1/audio/voice_consents'
VOICES = '/v1/audio/voices'
SECRET = '9' * 64  # Private loopback fixture, not a deployment credential.
LIMITS = dict(starts=1, posts=18, controls=2000, seconds=1200,
              response_bytes=4 << 20, artifact_mib=64, disk_headroom_mib=256)
# Attempts include metadata-only loading; they are not claimed as GPU loads.
LOAD_LIMITS = dict(native_tts_health=2, native_tts_base_health=3,
                   native_asr_health=4, native_speaker_health=2,
                   native_voice_health=1, native_vad_health=1)
PHRASE = ('I am the owner of this voice and I consent to OpenAI using this '
          'voice to create a synthetic voice model.')
SAMPLE = 'The meeting starts tomorrow morning. Please bring your notebook.'
TARGETS = ['The blue train arrives at noon.', 'Please close the window.']


def requests():
    clips = {}
    for name, text, voice in (
            ('consent', PHRASE, 'ryan'), ('sample', SAMPLE, 'ryan'),
            ('different', SAMPLE, 'vivian'),
            ('negated', PHRASE.replace('I consent', 'I do not consent'), 'ryan')):
        clips[name] = dict(model=speech.MODEL, input=text, voice=voice,
                           response_format='wav', stream_format='sse', speed=1)
    return dict(clips=clips, targets=TARGETS, core=chat.requests()['core'],
                transcription=dict(model='qwen3-asr-0.6b', response_format='json'),
                consent=dict(name='local-consent', language='en'),
                voice=dict(name='local-sample', type='audio_sample'),
                rename=dict(name='local-consent-renamed'))


def multipart(fields, field, audio):
    boundary = 'q4t-voice-business-20261118'
    require(boundary.encode() not in audio, 'binary boundary collision')
    parts = []
    for key, value in fields.items():
        require(isinstance(value, str), 'multipart values must be strings')
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; '
                      f'name="{key}"\r\n\r\n{value}\r\n').encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; '
                  f'name="{field}"; filename="recording.wav"\r\n'
                  'Content-Type: audio/wav\r\n\r\n').encode() + audio +
                 f'\r\n--{boundary}--\r\n'.encode())
    return b''.join(parts), 'multipart/form-data; boundary='+boundary


def check_wave(raw):
    with wave.open(io.BytesIO(raw), 'rb') as wav:
        require(wav.getnchannels() == 1 and wav.getsampwidth() == 2 and
                wav.getframerate() == 24000, 'unexpected native WAV format')
        count = wav.getnframes()
        require(1920 <= count <= 200*1920, 'WAV frame budget')
        data = wav.readframes(count)
        require(len(data) == count*2, 'truncated WAV')
    values = struct.unpack('<'+'h'*count, data)
    peak = max(abs(v) for v in values)/32768
    rms = (sum(float(v)*v for v in values)/count)**.5/32768
    require(peak > .01 and rms > .0001, 'silent output cannot pass')
    return dict(samples=count, rate=24000, seconds=count/24000,
                peak=peak, rms=rms, sha256=hashlib.sha256(raw).hexdigest())


def checked_error(status, raw, expected_status, code=None):
    value = json.loads(raw)
    require(status == expected_status and isinstance(value.get('error'), dict),
            'negative did not return its expected error envelope')
    require('id' not in value and value['error'].get('code') and
            (code is None or value['error']['code'] == code),
            'negative returned a successful ID or wrong error classification')
    return value


def checked_metadata(status, raw, kind):
    value = json.loads(raw)
    keys = {'id', 'object', 'created_at', 'name',
            'type' if kind == 'voice' else 'language'}
    require(status == 200 and set(value) == keys and
            value['object'] == 'audio.'+kind and
            value['id'].startswith('voice_' if kind == 'voice' else 'cons_') and
            type(value['created_at']) is int and value['created_at'] > 0,
            'missing metadata or private evidence leaked')
    return value


def check_transcript(value, expected):
    require(type(value) is dict and isinstance(value.get('text'), str) and
            normalize(value['text']) == normalize(expected),
            'independent unprompted ASR content differs')


def validate_plan(plan):
    require(plan['schema'] == 'q4t-voice-http-v1' and
            plan['limits'] == LIMITS and plan['load_limits'] == LOAD_LIMITS and
            plan['requests'] == requests(), 'frozen plan drift')


def prepare(args):
    require(not subprocess.check_output(['git', 'status', '--porcelain']),
            'commit reviewed driver before freezing')
    out = args.output.absolute()
    require(out.is_relative_to(ROOT/'.q4t-work'), 'private output only')
    out.mkdir(parents=True, mode=0o700, exist_ok=False)
    binary = args.binary.absolute()
    production = read(binary.parent/'chat_protocol_sources.json')
    for name, digest in production.items():
        require(sha(ROOT/name) == digest, 'built production source drift: '+name)
    directories = dict(tts=speech.WORK/'assets-1b7-01',
        base=WORK/'base-service-assets-01', asr=speech.ASR/'assets-01',
        speaker=ROOT/'.q4t-work/serve-diarization-20261118/native-assets-01',
        vad=ROOT/'.q4t-work/serve-audio-aligner-20261117/vad-01')
    names = subprocess.check_output(['git', 'ls-files', 'src', 'include', 'cmake',
        'CMakeLists.txt', 'tools/native_tts', 'tools/native_asr',
        'tools/serve_grammar_reap', 'tools/serve_protocol', 'tools/serve_recovery',
        'tools/serve_service', 'tools/evalscope', 'tools/examples', 'tools/release'],
        text=True).splitlines()
    model = Path.home()/'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
    plan = dict(schema='q4t-voice-http-v1', output=str(out), limits=LIMITS,
        load_limits=LOAD_LIMITS, requests=requests(), port=args.port,
        operations_port=args.port+1, model=str(model), model_assets=speech.assets(model),
        binaries=dict(engine=dict(path=str(binary), sha256=sha(binary))),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        sources={name: sha(ROOT/name) for name in names}, defaults=defaults(),
        production_sources=production, directories={k:str(v) for k,v in directories.items()},
        auxiliary_assets={k:{str(p.relative_to(v)):dict(bytes=p.stat().st_size,
            sha256=sha(p)) for p in sorted(v.rglob('*')) if p.is_file()}
            for k,v in directories.items()},
        acceptance=dict(no_asr_text_or_language_hint=True,
            one_core_pid=True, sample_text_differs_from_both_targets=True,
            same_speaker_cosine_minimum=.65, preserve_revocable_owner_scope=True,
            listening='PENDING', human_identity='NOT_CLAIMED',
            reference_audio_icl='PENDING', performance='NOT_CLAIMED'),
        mutations=['write only private fixture audio and owned object store',
                   'create, rename and revoke owned synthetic consent',
                   'create and delete owned sample voice; snapshot before revocation',
                   'gracefully stop only the owned model at stage exit'])
    require(all(plan['auxiliary_assets'].values()), 'missing auxiliary assets')
    save(out/'plan.json', plan)
    print(json.dumps(dict(plan=str(out/'plan.json'), limits=LIMITS)), flush=True)


class Stage(speech.Stage):
    def __init__(self, plan):
        require(not os.environ.get('PYTHONOPTIMIZE') and not any(
            k.startswith('Q4T_ABLATE_') for k in os.environ), 'unqualified environment')
        validate_plan(plan)
        self.plan, self.out = plan, Path(plan['output'])
        self.started = time.monotonic()
        self.counts = dict(starts=0, posts=0, controls=0)
        self.model = self.model_identity = self.fault_thread = None
        self.history = {}
        self.lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
        lifecycle.verify_lease(self.lease, lifecycle.guard.common_lock())
        for name, digest in plan['sources'].items():
            require(sha(ROOT/name) == digest, 'source drift: '+name)
        for row in plan['binaries'].values():
            require(sha(row['path']) == row['sha256'], 'binary drift')
        require(defaults() == plan['defaults'], 'default drift')
        speech.check_assets(plan['model'], plan['model_assets'])
        for kind, files in plan['auxiliary_assets'].items():
            for name, row in files.items():
                path = Path(plan['directories'][kind])/name
                require(path.stat().st_size == row['bytes'] and sha(path) == row['sha256'],
                        'auxiliary asset drift: '+str(path))
        self.artifacts()

    def artifacts(self):
        require(sum(p.stat().st_size for p in self.out.rglob('*') if p.is_file())
                <= LIMITS['artifact_mib'] << 20, 'artifact budget')
        require(shutil.disk_usage(ROOT).free >= LIMITS['disk_headroom_mib'] << 20,
                'disk headroom')

    def budget(self, kind):
        require(time.monotonic()-self.started < LIMITS['seconds']-40, 'stage deadline')
        require(self.counts[kind] < LIMITS[kind], 'attempt budget: '+kind)
        self.artifacts()
        self.counts[kind] += 1
        save(self.out/'counts.json', self.counts)

    def http(self, path, body=None, *, method=None, label=None, client='audio',
             content_type='application/json', timeout=180, business=False):
        method = method or ('GET' if body is None else 'POST')
        self.budget('posts' if business else 'controls')
        local = method == 'GET' and path in ('/readyz', '/metrics', '/v1/config')
        headers = {} if local else {'X-Q4T-Client-Id':client,
                                   'X-Q4T-Gateway-Token':SECRET}
        headers.update({'Content-Type':content_type, 'Connection':'close'})
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        if label:
            save(self.out/(label+'.request.json'), dict(method=method, path=path,
                client=None if local else client, content_type=content_type,
                body_bytes=0 if body is None else len(body),
                body_sha256=None if body is None else hashlib.sha256(body).hexdigest()))
            if body is not None:
                (self.out/(label+'.request.body')).write_bytes(body)
        port = self.plan['operations_port'] if local else self.plan['port']
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=min(timeout,
            max(1, LIMITS['seconds']-40-(time.monotonic()-self.started))))
        started = time.monotonic()
        try:
            conn.request(method, path, body, headers)
            response = conn.getresponse()
            raw = response.read(LIMITS['response_bytes']+1)
            if label:
                (self.out/(label+'.wire')).write_bytes(raw)
                save(self.out/(label+'.http.json'), dict(status=response.status,
                    headers=dict(response.getheaders()), bytes=len(raw),
                    seconds=time.monotonic()-started))
            require(len(raw) <= LIMITS['response_bytes'], 'response bound exceeded')
            return response.status, raw
        finally:
            conn.close()

    def observe(self, label):
        self.same_model()
        self.drained(label+'-drain')
        status, raw = self.http('/readyz', label=label+'-ready')
        require(status == 200, 'auxiliary request poisoned core readiness')
        status, raw = self.http('/v1/config', label=label+'-config')
        require(status == 200, 'health observation failed')
        config = json.loads(raw)
        for key, limit in LOAD_LIMITS.items():
            state = config[key]
            require(0 <= state['load_attempts'] <= limit, 'load bound: '+key)
            require(state['load_failures'] == 0 and state['compute_failures'] == 0,
                    'unexpected auxiliary failure: '+key)
        self.artifacts()
        return config

    def post(self, label, path, body, **kwargs):
        result = self.http(path, body, label=label, business=True, **kwargs)
        self.observe(label)
        print(json.dumps(dict(step=label, status=result[0], counts=self.counts)), flush=True)
        return result

    def start(self):
        chat.probe_ports((self.plan['port'], self.plan['operations_port']))
        policy = dict(version=3, revision=0, fair_queue=True, gateway_token=SECRET,
            clients=[dict(id=name, max_inflight=1, max_control_inflight=4,
                max_prompt_tokens=8192, max_output_tokens=8192,
                max_body_bytes=(10 << 20)+65536, requests_per_minute=32,
                tokens_per_minute=64 if name == 'tiny' else 200000)
                for name in ('audio', 'other', 'tiny')])
        save(self.out/'client-policy.json', policy)
        command = [self.plan['binaries']['engine']['path'], 'serve', '--model-dir',
            self.plan['model'], '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']), '--mtp',
            '--max-seq', '1', '--max-len', '8192', '--max-prefill', '2048',
            '--max-tokens', '64', '--max-inflight-requests', '2',
            '--max-queued-requests', '1', '--object-store-dir', str(self.out/'objects'),
            '--client-policy-file', str(self.out/'client-policy.json'),
            '--tts-max-text-tokens', '512', '--tts-max-prefill-tokens', '512',
            '--tts-max-frames', '200', '--tts-workspace-mib', '2048',
            '--tts-max-output-mib', '16', '--asr-max-output-tokens', '256',
            '--speaker-max-windows', '32', '--vad-max-chunks', '8']
        for key, option in (('tts','tts-model-dir'), ('base','tts-base-model-dir'),
                ('asr','asr-model-dir'), ('speaker','speaker-model-dir'), ('vad','vad-model-dir')):
            command += ['--'+option, self.plan['directories'][key]]
        self.budget('starts')
        save(self.out/'model-command.json', command)
        with (self.out/'model.log').open('xb', buffering=0) as log:
            self.model = subprocess.Popen(command, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.model_identity = lifecycle.guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out/'model-identity.json', dict(pid=self.model.pid, **self.model_identity))
        chat.Stage.wait_ready(self)
        status, raw = self.http('/v1/capabilities', label='initial-capabilities')
        require(status == 200 and json.loads(raw)['voice_registration']['enabled'],
                'configured registration capability missing')
        self.observe('startup')

    def core(self, label):
        status, raw = self.post(label, '/v1/chat/completions', self.plan['requests']['core'])
        require(status == 200, 'core HTTP failure')
        value = json.loads(raw)['choices'][0]
        require(value['finish_reason'] == 'stop' and
                value['message']['content'].strip() == '42', 'core answer differs')

    def form(self, label, path, fields, field, audio, **kwargs):
        body, mime = multipart(fields, field, audio)
        return self.post(label, path, body, content_type=mime, **kwargs)

    def transcribe(self, label, raw, target):
        # Target is only compared after HTTP. No prompt/language in this form.
        status, wire = self.form(label, '/v1/audio/transcriptions',
            self.plan['requests']['transcription'], 'file', raw)
        require(status == 200, 'independent ASR HTTP failure')
        value = json.loads(wire)
        save(self.out/(label+'.quality.json'), dict(expected=target, response=value))
        check_transcript(value, target)

    def snapshot(self, consent, voice, completion):
        paths = sorted(p for p in (self.out/'objects').rglob('*') if p.is_file())
        require(0 < len(paths) <= 512 and sum(p.stat().st_size for p in paths) <= 16 << 20,
                'preserved-state snapshot bound')
        result = {}
        for path in paths:
            require(not path.is_symlink(), 'object snapshot symlink')
            name = path.relative_to(self.out/'objects')
            target = self.out/'registered-state'/name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(path, target)
            result[str(name)] = sha(target)
        save(self.out/'registered-state.json', dict(files=result, consent=consent,
            voice=voice, completion=completion, isolated_fixture=True,
            use='preserved-state compatible rollback; no default state mutation'))

    def business(self):
        self.core('core-before')
        clips = {}
        for label, body in self.plan['requests']['clips'].items():
            status, raw = self.post('fixture-'+label, '/v1/audio/speech', body)
            require(status == 200, 'fixture synthesis failed: '+label)
            audio, usage = speech.sse(raw)
            save(self.out/('fixture-'+label+'.audio.json'), dict(**check_wave(audio), **usage))
            (self.out/('fixture-'+label+'.wav')).write_bytes(audio)
            clips[label] = audio
        fields = self.plan['requests']['consent']
        status, raw = self.form('negated-consent', CONSENTS, fields, 'recording', clips['negated'])
        checked_error(status, raw, 400, 'voice_consent_mismatch')
        status, raw = self.form('consent', CONSENTS, fields, 'recording', clips['consent'])
        consent = checked_metadata(status, raw, 'voice_consent')['id']
        status, raw = self.http(CONSENTS+'/'+consent, self.plan['requests']['rename'], label='rename')
        require(checked_metadata(status, raw, 'voice_consent')['name'] == 'local-consent-renamed',
                'rename did not persist')
        status, raw = self.http(CONSENTS+'/'+consent, label='consent-other', client='other')
        checked_error(status, raw, 404)
        status, raw = self.http(CONSENTS+'?limit=1', label='consent-list')
        require(status == 200 and json.loads(raw)['data'][0]['id'] == consent,
                'registered consent missing from list')
        voice_fields = dict(self.plan['requests']['voice'], consent=consent)
        status, raw = self.form('voice-other', VOICES, voice_fields, 'audio_sample',
                                clips['sample'], client='other')
        checked_error(status, raw, 404)
        status, raw = self.form('voice-mismatch', VOICES, voice_fields, 'audio_sample', clips['different'])
        checked_error(status, raw, 400, 'voice_speaker_mismatch')
        status, raw = self.form('voice', VOICES, voice_fields, 'audio_sample', clips['sample'])
        voice = checked_metadata(status, raw, 'voice')['id']
        status, raw = self.form('quota', CONSENTS, fields, 'recording', clips['consent'], client='tiny')
        checked_error(status, raw, 429, 'client_tpm_exhausted')
        request = dict(model=BASE, input=TARGETS[0], voice=dict(id=voice),
                       response_format='wav', stream_format='sse')
        status, raw = self.post('speech-other', '/v1/audio/speech', request, client='other')
        checked_error(status, raw, 404)
        status, raw = self.post('base-speech', '/v1/audio/speech', request)
        require(status == 200, 'registered Base Speech HTTP failed')
        audio, usage = speech.sse(raw)
        save(self.out/'base-speech.audio.json', dict(**check_wave(audio), **usage))
        (self.out/'base-speech.wav').write_bytes(audio)
        self.transcribe('base-speech-asr', audio, TARGETS[0])
        body = chat.request('Repeat exactly without adding any words: '+TARGETS[1], 'wav')
        body['audio']['voice'] = dict(id=voice)
        body['store'] = True
        status, raw = self.post('base-chat', '/v1/chat/completions', body)
        require(status == 200, 'registered Base Chat HTTP failed')
        result = json.loads(raw)
        require(len(result['choices']) == 1 and result['choices'][0]['finish_reason'] == 'stop',
                'partial Chat synthesis')
        sound = result['choices'][0]['message']['audio']
        chat.check_usage(result['usage'], 1)
        require(normalize(sound['transcript']) == normalize(TARGETS[1]), 'Chat text differs')
        audio = base64.b64decode(sound['data'], validate=True)
        save(self.out/'base-chat.audio.json', check_wave(audio))
        (self.out/'base-chat.wav').write_bytes(audio)
        self.transcribe('base-chat-asr', audio, TARGETS[1])
        self.snapshot(consent, voice, result['id'])
        status, raw = self.http(CONSENTS+'/'+consent, method='DELETE', label='revoke')
        require(status == 200 and json.loads(raw).get('deleted') is True, 'consent not revoked')
        status, raw = self.post('revoked-speech', '/v1/audio/speech', request)
        checked_error(status, raw, 404)
        status, raw = self.http(CONSENTS+'/'+consent, label='revoked-consent')
        checked_error(status, raw, 404)
        status, raw = self.http(VOICES+'/'+voice, method='DELETE', label='delete-voice')
        require(status == 200 and json.loads(raw).get('deleted') is True, 'voice not deleted')
        self.core('core-after')
        require(self.counts['posts'] == LIMITS['posts'], 'business checklist incomplete')

    def run(self):
        result = dict(status='FAILED')
        previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(
            TimeoutError('voice stage deadline; cleanup reserve retained')))
        signal.alarm(LIMITS['seconds']-40)
        try:
            self.start()
            self.business()
            result['status'] = 'PASS_VOICE_SAMPLE_HTTP_SCOPE_CONTENT_REVOKE'
        except BaseException:
            result['error'] = traceback.format_exc()
            raise
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
            try:
                self.cleanup()
                require(defaults() == self.plan['defaults'], 'default changed')
                self.artifacts()
            except BaseException:
                result['status'] = 'FAILED'
                result['cleanup_error'] = traceback.format_exc()
                raise
            finally:
                result.update(counts=self.counts, seconds=time.monotonic()-self.started,
                    defaults_unchanged=defaults() == self.plan['defaults'],
                    acceptance=self.plan['acceptance'])
                save(self.out/'result.json', result)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='mode', required=True)
    prepare_parser = sub.add_parser('prepare')
    prepare_parser.add_argument('--binary', type=Path, required=True)
    prepare_parser.add_argument('--output', type=Path, required=True)
    prepare_parser.add_argument('--port', type=int, default=18278)
    run = sub.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        with lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
