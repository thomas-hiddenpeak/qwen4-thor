"""Exercise the issued speech backend and transcribe its actual MPEG response."""
import base64
import hashlib
import http.client
import json
from pathlib import Path
import re

from tools.serve_recovery import profile_evidence
from tools.serve_soak.native_contract import require

MAX_AUDIO = 1024 * 1024
LABELS = ('speech-request.json', 'speech.mp3', 'transcription-request.bin',
          'transcription.json', 'speech-http.json', 'transcription-http.json')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def binding(package, manifest):
    profiles = manifest['recovery_profiles']
    identity = profiles['identity']
    asr = identity.get('native_asr')
    require(asr and identity.get('native_tts'), 'issued audio backends required')
    raw = (Path(package)/'profile-primary.json').read_bytes()
    require(digest(raw) == profiles['qualifications']['primary']['evidence_sha256'],
            'audio reference differs from the registered primary profile')
    sample = json.loads(raw)['business']['tts']
    request = profile_evidence.tts_requests()['speech']
    require(request == sample['requests']['speech'] and request['input'].strip(),
            'reviewed speech request differs')
    profile_evidence.tts_audio(sample['speech_base64'], 'speech',
                               sampling_profile=sample.get('sampling_profile'))
    data = base64.b64decode(sample['speech_base64'], validate=True)
    return dict(speech=request, bytes=len(data), sha256=digest(data),
                asr_model=asr['model'])


def multipart(audio, model):
    # Use a deterministic boundary absent from the payload so the independent
    # report can reconstruct the entire submitted input, including its headers.
    boundary = 'q4t-' + digest(audio)
    require(boundary.encode() not in audio, 'multipart boundary in speech')
    start = ('--' + boundary + '\r\n').encode()
    def field(name, value):
        return start + f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    body = (field('model', model) + field('response_format', 'json') + start
            + b'Content-Disposition: form-data; name="file"; filename="speech.mp3"\r\n'
            + b'Content-Type: audio/mpeg\r\n\r\n' + audio
            + ('\r\n--' + boundary + '--\r\n').encode())
    return body, 'multipart/form-data; boundary=' + boundary


def post(stage, label, route, content_type, body, limit):
    stage.budget.reserve('chats', label)
    conn = http.client.HTTPConnection('127.0.0.1', stage.port, timeout=120)
    try:
        conn.request('POST', route, body, {'Authorization': 'Bearer ' + stage.keys['alpha'],
                                          'Content-Type': content_type})
        response = conn.getresponse()
        wire = response.read(limit + 1)
        require(len(wire) <= limit, label + ' exceeds response bound')
        return dict(status=response.status, type=response.getheader('Content-Type', ''),
                    request_sha256=digest(body)), wire
    finally:
        conn.close()


def verify_speech(reference, audio, response):
    require(response['status'] == 200 and response['type'].split(';')[0].lower() == 'audio/mpeg'
            and len(audio) == reference['bytes'] and digest(audio) == reference['sha256'],
            'synthesized audio differs from the issued speech contract')


def verify(reference, path):
    def data(name): return path.with_name(path.name + '-' + name).read_bytes()
    speech_request = data('speech-request.json')
    require(json.loads(speech_request) == reference['speech'], 'speech input changed')
    audio = data('speech.mp3')
    speech = json.loads(data('speech-http.json'))
    verify_speech(reference, audio, speech)
    upload, mime = multipart(audio, reference['asr_model'])
    receipt = json.loads(data('transcription-http.json'))
    require(data('transcription-request.bin') == upload and receipt['request_sha256'] == digest(upload)
            and receipt['input_type'] == mime and speech['request_sha256'] == digest(speech_request),
            'ASR input differs from the synthesized clip')
    require(receipt['status'] == 200 and receipt['type'].split(';')[0].lower() == 'application/json',
            'transcription HTTP response differs')
    text = json.loads(data('transcription.json'))['text']
    require(isinstance(text, str) and re.findall(r'\w+', text.casefold()) ==
            re.findall(r'\w+', reference['speech']['input'].casefold()),
            'ASR did not recover the spoken sentence')


def exercise(stage, prefix, reference):
    path = stage.output / prefix
    def write(name, data):
        with path.with_name(prefix + '-' + name).open('xb') as f:
            f.write(data)
    body = json.dumps(reference['speech']).encode()
    write('speech-request.json', body)
    speech, audio = post(stage, prefix+'-speech', '/v1/audio/speech', 'application/json', body, MAX_AUDIO)
    write('speech-http.json', json.dumps(speech).encode())
    write('speech.mp3', audio)
    verify_speech(reference, audio, speech)
    upload, mime = multipart(audio, reference['asr_model'])
    write('transcription-request.bin', upload)
    transcription, wire = post(stage, prefix+'-asr', '/v1/audio/transcriptions', mime, upload, 65536)
    write('transcription.json', wire)
    write('transcription-http.json', json.dumps(dict(transcription, input_type=mime)).encode())
    verify(reference, path)
