"""Actual registered-voice lifecycle for each owned primary/light profile.

Fixture files are inputs only. Registration, synthesis, independent ASR and
revocation must be observed from the running engine; no retained success is
substituted for a request. This module never starts or restarts the model.
"""
import base64
import copy
import hashlib
import http.client
import io
import json
import re
from pathlib import Path
import time
import wave

from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import ledger
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_service import capacity
from tools.serve_service.package import read, require, save, sha

FORMAT = 'q4t-profile-voice-inputs-v1'
NAMES = ('consent', 'sample', 'different', 'negated')
POSTS_PER_PROFILE = 12
MAX_AUDIO_BYTES = 10 << 20
MAX_RESPONSE_BYTES = 4 << 20
CONSENTS = '/v1/audio/voice_consents'
VOICES = '/v1/audio/voices'


def fixtures(path):
    value = read(path)
    ledger.keys(value, ('format', 'clips'))
    require(value['format'] == FORMAT, 'invalid voice fixture manifest')
    ledger.keys(value['clips'], NAMES)
    result = {}
    for name, row in value['clips'].items():
        ledger.keys(row, ('path', 'sha256'))
        p = Path(row['path'])
        require(p.is_absolute() and type(row['sha256']) is str and
            ledger.HASH.fullmatch(row['sha256']), 'invalid voice fixture identity')
        require(0 < p.stat().st_size <= MAX_AUDIO_BYTES and sha(p) == row['sha256'],
                'voice fixture size or identity differs: '+name)
        raw = p.read_bytes()
        with wave.open(io.BytesIO(raw)) as wav:
            count, rate = wav.getnframes(), wav.getframerate()
            require(wav.getnchannels() == 1 and wav.getsampwidth() == 2 and
                rate in (16000, 24000) and 1 <= count <= 60*rate and
                len(wav.readframes(count)) == count*2, 'invalid voice fixture WAV')
        result[name] = dict(path=str(p), sha256=row['sha256'])
    require(sum(Path(row['path']).stat().st_size for row in result.values()) <= 8 << 20,
            'voice fixture aggregate byte bound')
    require(len({row['sha256'] for row in result.values()}) == len(NAMES),
            'voice fixture negatives cannot alias positive recordings')
    return result


def client_policy(value, profile, tts):
    value = copy.deepcopy(value)
    require(len(value['clients']) == 1, 'voice driver expects its one primary client')
    primary = value['clients'][0]
    primary.update(max_inflight=1, max_prompt_tokens=16384,
        max_output_tokens=max(1536, profile['max_tokens']+tts['limits']['max_frames']*16+1),
        max_body_bytes=MAX_AUDIO_BYTES+65536,
        requests_per_minute=max(primary['requests_per_minute'], 32),
        tokens_per_minute=max(primary['tokens_per_minute'], 200000))
    value['clients'] += [dict(primary, id='voice-other'),
                         dict(primary, id='voice-tiny', tokens_per_minute=64)]
    capacity.check_clients(profile, value)
    return value


def multipart(fields, field, raw):
    boundary = 'q4t-profile-registered-voice'
    require(type(raw) is bytes and boundary.encode() not in raw,
            'invalid voice upload or multipart boundary collision')
    pieces = []
    for key, value in fields.items():
        require(type(value) is str and '\r' not in key and '\n' not in key,
                'invalid voice multipart field')
        pieces.append((f'--{boundary}\r\nContent-Disposition: form-data; '
                       f'name="{key}"\r\n\r\n{value}\r\n').encode())
    pieces.append((f'--{boundary}\r\nContent-Disposition: form-data; '
                   f'name="{field}"; filename="recording.wav"\r\n'
                   'Content-Type: audio/wav\r\n\r\n').encode()+raw+
                  f'\r\n--{boundary}--\r\n'.encode())
    return b''.join(pieces), 'multipart/form-data; boundary='+boundary


class Lifecycle:
    def __init__(self, stage):
        self.stage = stage
        self.requests = 0
        self.clips = {}
        for name, row in stage.plan['voice_clips'].items():
            raw = Path(row['path']).read_bytes()
            require(len(raw) <= MAX_AUDIO_BYTES and hashlib.sha256(raw).hexdigest() ==
                    row['sha256'], 'voice fixture drift before request')
            self.clips[name] = raw
        require(set(self.clips) == set(NAMES), 'incomplete voice fixture inventory')

    def http(self, label, path, body=None, *, method=None, client=None,
             content_type='application/json'):
        stage = self.stage
        method = method or ('GET' if body is None else 'POST')
        business = method == 'POST'
        stage.budget('chats' if business else 'controls')
        if business:
            self.requests += 1
            require(self.requests <= POSTS_PER_PROFILE, 'voice business request bound')
        headers = dict(encrypted.HEADERS)
        if client is not None:headers['X-Q4T-Client-Id'] = client
        headers.update({'Content-Type':content_type, 'Connection':'close'})
        if body is not None and type(body) is not bytes:
            body = json.dumps(body, ensure_ascii=False).encode()
        prefix = stage.out/(stage.profile_name+'-voice-'+label)
        save(Path(str(prefix)+'.request.json'), dict(method=method, path=path,
            client=headers['X-Q4T-Client-Id'], content_type=content_type,
            body_bytes=0 if body is None else len(body),
            body_sha256=None if body is None else hashlib.sha256(body).hexdigest()))
        if body is not None:Path(str(prefix)+'.request.body').write_bytes(body)
        conn = http.client.HTTPConnection('127.0.0.1', stage.plan['port'], timeout=120)
        started = time.monotonic()
        try:
            conn.request(method, path, body, headers)
            response = conn.getresponse();raw = response.read(MAX_RESPONSE_BYTES+1)
            Path(str(prefix)+'.wire').write_bytes(raw)
            save(Path(str(prefix)+'.http.json'), dict(status=response.status,
                headers=dict(response.getheaders()), seconds=time.monotonic()-started))
            require(len(raw) <= MAX_RESPONSE_BYTES, 'voice response byte bound')
        finally:
            conn.close()
        stage.same_model();stage.drained(stage.profile_name+'-voice-'+label+'-drain')
        return response.status, raw

    def form(self, label, path, fields, field, audio, **options):
        body, content_type = multipart(fields, field, audio)
        return self.http(label, path, body, content_type=content_type, **options)

    @staticmethod
    def rejected(result, status, code=None):
        value = dict(status=result[0], body=json.loads(result[1]))
        evidence.voice_error(value, status, code)
        return value

    @staticmethod
    def accepted(result):
        require(result[0] == 200, 'registered voice business request failed')
        return json.loads(result[1])

    @classmethod
    def created(cls, result, kind):
        value = cls.accepted(result)
        field = 'language' if kind == 'voice_consent' else 'type'
        ledger.keys(value, ('id', 'object', 'created_at', 'name', field))
        prefix = 'cons_' if kind == 'voice_consent' else 'voice_'
        require(type(value['id']) is str and value['id'].startswith(prefix) and
            len(value['id']) > len(prefix) and len(value['id']) <= 128 and
            re.fullmatch(r'[A-Za-z0-9_-]+', value['id']) and
            value['object'] == 'audio.'+kind and
            ledger.integer(value['created_at'], minimum=1),
            'invalid created voice metadata or unsafe returned ID')
        return value

    def run(self):
        from tools.native_tts.http_acceptance import sse
        fields = evidence.voice_requests('', '')['consent']
        negative = dict(negated_consent=self.rejected(self.form('negated', CONSENTS,
            fields, 'recording', self.clips['negated']), 400, 'voice_consent_mismatch'))
        consent = self.created(self.form('consent', CONSENTS,
            fields, 'recording', self.clips['consent']), 'voice_consent')
        consent_id = consent['id']
        rows = evidence.voice_requests(consent_id, '')
        negative['cross_consent'] = self.rejected(self.http('cross-consent',
            CONSENTS+'/'+consent_id, client='voice-other'), 404)
        negative['cross_voice'] = self.rejected(self.form('cross-create', VOICES,
            rows['voice'], 'audio_sample', self.clips['sample'], client='voice-other'), 404)
        negative['different_speaker'] = self.rejected(self.form('different', VOICES,
            rows['voice'], 'audio_sample', self.clips['different']), 400, 'voice_speaker_mismatch')
        voice = self.created(self.form('create', VOICES, rows['voice'],
            'audio_sample', self.clips['sample']), 'voice')
        rows = evidence.voice_requests(consent_id, voice['id'])
        negative['quota'] = self.rejected(self.form('quota', CONSENTS, fields,
            'recording', self.clips['consent'], client='voice-tiny'), 429, 'client_tpm_exhausted')
        negative['cross_speech'] = self.rejected(self.http('cross-speech',
            '/v1/audio/speech', rows['speech'], client='voice-other'), 404)
        status, wire = self.http('speech', '/v1/audio/speech', rows['speech'])
        require(status == 200, 'registered voice Speech failed')
        raw, usage = sse(wire)
        speech = dict(audio_base64=base64.b64encode(raw).decode(), usage=usage['usage'])
        speech_transcript = self.accepted(self.form('speech-asr', '/v1/audio/transcriptions',
            rows['transcription'], 'file', raw))
        require(speech_transcript.get('text') == evidence.VOICE_TARGETS[0],
                'registered Speech independent transcription differs')
        chat = self.accepted(self.http('chat', '/v1/chat/completions', rows['chat']))
        message = evidence.message(chat, 'stop')
        stored = self.accepted(self.http('stored-chat',
            '/v1/chat/completions/'+chat['id']))
        saved = evidence.message(stored, 'stop')
        require(stored.get('id') == chat['id'] and saved.get('audio', {}).get('id') ==
                message['audio']['id'] and saved['audio'].get('transcript') ==
                message['audio']['transcript'], 'registered Chat audio history not retained')
        raw = base64.b64decode(message['audio']['data'], validate=True)
        chat_transcript = self.accepted(self.form('chat-asr', '/v1/audio/transcriptions',
            rows['transcription'], 'file', raw))
        require(chat_transcript.get('text') == evidence.VOICE_TARGETS[1],
                'registered Chat independent transcription differs')
        revoked = dict(consent_deleted=self.accepted(self.http('revoke',
            CONSENTS+'/'+consent_id, method='DELETE')))
        revoked['speech'] = self.rejected(self.http('revoked-speech',
            '/v1/audio/speech', rows['speech']), 404)
        revoked['consent'] = self.rejected(self.http('revoked-consent',
            CONSENTS+'/'+consent_id), 404)
        revoked['voice_deleted'] = self.accepted(self.http('delete',
            VOICES+'/'+voice['id'], method='DELETE'))
        status, raw = self.stage.http('/v1/config',
            label=self.stage.profile_name+'-voice-configuration')
        require(status == 200, 'voice health observation unavailable')
        config = json.loads(raw)
        result = dict(requests=rows, consent=consent, voice=voice, speech=speech,
            speech_transcript=speech_transcript, chat=chat, chat_transcript=chat_transcript,
            negative=negative, revocation=revoked, limits=config['native_tts_limits'],
            health=dict(base=config['native_tts_base_health'],
                        condition=config['native_voice_health']))
        require(self.requests == POSTS_PER_PROFILE, 'incomplete voice business checklist')
        evidence.voice_business(result, self.stage.plan['identity'])
        return result
