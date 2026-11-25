"""Verify retained profile observations and actual business result contracts."""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import io
import itertools
import json
import math
import re
import wave
import zlib

from tools.serve_recovery import ledger, profile_contract

TTS_FORMAT = 'q4t-profile-business-v8'
VOICE_FORMAT = 'q4t-profile-business-v9'
TTS_CHECKPOINT_FORMAT = 'q4t-profile-business-v10'
VOICE_CHECKPOINT_FORMAT = 'q4t-profile-business-v11'
VOICE_CONSUMER_FORMAT = 'q4t-profile-business-v12'
VOICE_ICL_FORMAT = 'q4t-profile-business-v13'
REALTIME_FORMAT = 'q4t-profile-business-v15'
SEMANTIC_FORMAT = 'q4t-profile-business-v16'
CONVERSATION_FORMAT = 'q4t-profile-business-v17'
PROMPT_FORMAT = 'q4t-profile-business-v18'
SPEECH_FORMAT = 'q4t-profile-business-v19'
DUPLEX_FORMAT = 'q4t-profile-business-v20'
MCP_FORMAT = 'q4t-profile-business-v21'
TRACING_FORMAT = 'q4t-profile-business-v22'
REALTIME_INPUT_SHA = '1a14e32fabaa9602179352659aabfd65ba85ff0e2daab337389cb04ec8d95fbc'
REALTIME_WAVEFORM_SHA = '7b84bf310eb247a51433720050487c9a79ea8d87505a8c24e83a52a4067ed71f'
# Exact saved state and full outputs from the accepted official SDK/TLS run.
# Both full WAVs were independently transcribed, then reproduced byte for byte
# by both registered-voice profiles. These are content witnesses, not new ASR.
VOICE_CONSUMER_WITNESS = dict(
    format='q4t-voice-consumer-witness-v1', owner='audio',
    consent='cons_152992b04ee1d2169519b5a25bf7c320',
    voice='voice_d91ef0c25a252f9ae75a631901960d90',
    state_sha256='b7b75d0ee358f87ac73cc61e982a8c42319165aec92da34572a982e4d7b2e53f',
    condition_sha256='9781f722ad38343deeea05a21d7c5700e0344b3703c1ce0d44e88d9173e16e12',
    evidence_sha256='fd2c3e8ce9fe74c84a76cd920c0b2380cf582c3c830ed99db31ee41132766af3')
VOICE_CONSUMER_AUDIO = dict(
    speech=(111404, '58dd9c9389ce28237d37c612e6a1208739bdaf8dd5ec3fcf2db0cbaae3c3176d'),
    chat=(76844, '15324b851c3ebca829b9d73b3848d590a3a657cc46e49c0277e7cef473b0fe0f'))
TTS_CHECKPOINT_SAMPLING = 'qwen3-tts-checkpoint-seed0-v1'
VOICE_TARGETS = ('The blue train arrives at noon.', 'Please close the window.')
TTS_TEXT = 'Hello, this is a native speech test.'
# Whole byte identities from the accepted native full-text synthesis + no-hint
# ASR in serve-chat-audio-output-http-20261118; no tolerances or shortened clips.
TTS_AUDIO = dict(
    speech=(40704, '5fc3c9bf612687dbf61badf607046f3fa8157d92246610059a66df6c47e29323'),
    chat=(115244, '4b71ea245be97ca9d2d3f85b0f67594d46a71f9cb2a5bf41cfacaa06efaf0126'))

# Independently recognized whole HTTP audio, fixed requests/seed; the original
# greedy v8/v9 witnesses above are immutable. See the 2026-11-19 checkpoint
# witness evidence, including its preserved driver failure and content-only run.
TTS_CHECKPOINT_AUDIO = dict(
    speech=(58368, 'ba9ca74cd842909ce3422d42681bcdfacc3d7d09e1247218981ce683f226d9c4'),
    chat=(111404, '2f2d4f92ccd99c0b55e2aeb18a30fce82b74d1d434c540038c931d1e080246b9'))


def tts_sampling(value):
    """Missing marks historical greedy evidence; explicit unknowns fail closed."""
    ledger.require(type(value) is dict, 'invalid TTS sampling witness')
    if 'sampling_profile' not in value:
        return None
    ledger.require(value['sampling_profile'] == TTS_CHECKPOINT_SAMPLING,
                   'unknown TTS sampling witness')
    return value['sampling_profile']


def tts_requests():
    from tools.serve_service import package
    return dict(speech=dict(model=package.NATIVE_TTS['model'], input=TTS_TEXT,
        voice='ryan', instructions='Speak calmly.', response_format='mp3',
        stream_format='audio', speed=1),
        chat=dict(model='qwen3.8-flash-next', temperature=0, enable_thinking=False,
            messages=[dict(role='user', content='Repeat exactly this sentence without adding words: '+TTS_TEXT)],
            modalities=['audio'], audio=dict(voice='ryan', format='wav'),
            stream=False, store=True))


def tts_audio(value, kind, *, sampling_profile=None):
    ledger.require(type(value) is str and 0 < len(value) <= 1024*1024,
                   'invalid qualified TTS audio encoding')
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise ValueError('invalid qualified TTS base64') from error
    ledger.require(sampling_profile in (None, TTS_CHECKPOINT_SAMPLING),
                   'unknown TTS audio sampling witness')
    accepted = TTS_AUDIO if sampling_profile is None else TTS_CHECKPOINT_AUDIO
    size, digest = accepted[kind]
    ledger.require(base64.b64encode(raw).decode() == value and len(raw) == size and
        hashlib.sha256(raw).hexdigest() == digest,
        'qualified TTS whole audio differs from accepted content')


def tts_business(value, identity):
    sampling = tts_sampling(value)
    ledger.keys(value, ('requests', 'speech_base64', 'chat_response', 'health', 'limits') +
                (('sampling_profile',) if sampling is not None else ()))
    fixed = identity['native_tts']
    profile_contract.tts_configuration(fixed)
    ledger.require(ledger.canonical(value['requests']) == ledger.canonical(tts_requests()) and
        ledger.canonical(value['limits']) == ledger.canonical(fixed['limits']),
        'qualified TTS requests or effective budgets differ')
    health = value['health']
    ledger.require(type(health) is dict and health.get('state') == 'ready' and
        health.get('model') == fixed['model'] and
        health.get('core_readiness_dependency') is False and
        health.get('reason') == 'none' and
        type(health.get('compute_failures')) is int and health['compute_failures'] == 0 and
        ledger.integer(health.get('device_bytes'), minimum=1),
        'qualified TTS backend is not healthy/isolated')
    tts_audio(value['speech_base64'], 'speech', sampling_profile=sampling)
    response = value['chat_response']
    msg = message(response, 'stop')
    ledger.require(msg.get('content') is None and type(msg.get('audio')) is dict,
                   'qualified Chat audio output is missing')
    audio = msg['audio']
    ledger.require(type(audio.get('id')) is str and audio['id'].startswith('audio_') and
        ledger.integer(audio.get('expires_at'), minimum=1) and
        audio.get('transcript') == TTS_TEXT, 'qualified Chat audio history differs')
    tts_audio(audio.get('data'), 'chat', sampling_profile=sampling)
    usage = response.get('usage', {})
    ledger.require(type(usage) is dict, 'invalid qualified TTS usage')
    parts = usage.get('completion_tokens_details', {})
    synthesis = usage.get('q4t_audio_synthesis', {})
    ledger.require(all(type(v) is dict for v in (usage, parts, synthesis)) and
        all(ledger.integer(usage.get(k), minimum=1) for k in
            ('prompt_tokens', 'completion_tokens', 'total_tokens')) and
        usage['total_tokens'] == usage['prompt_tokens']+usage['completion_tokens'] and
        ledger.integer(parts.get('audio_tokens'), minimum=16) and
        parts['audio_tokens'] % 16 == 0 and
        ledger.integer(synthesis.get('input_tokens'), minimum=1) and
        synthesis.get('output_tokens') == parts['audio_tokens']+1 and
        usage['completion_tokens'] > synthesis['output_tokens'] and
        usage['prompt_tokens'] > synthesis['input_tokens'],
        'qualified TTS actual usage missing or inconsistent')


def voice_requests(consent, voice):
    from tools.serve_service import package
    return dict(consent=dict(name='local-consent', language='en'),
        voice=dict(name='local-sample', type='audio_sample', consent=consent),
        speech=dict(model=package.NATIVE_TTS_BASE['model'], input=VOICE_TARGETS[0],
            voice=dict(id=voice), response_format='wav', stream_format='sse'),
        chat=dict(model='qwen3.8-flash-next', temperature=0, enable_thinking=False,
            max_completion_tokens=1536, messages=[dict(role='user',
                content='Repeat exactly without adding any words: '+VOICE_TARGETS[1])],
            modalities=['text', 'audio'], audio=dict(voice=dict(id=voice), format='wav'),
            stream=False, store=True),
        transcription=dict(model='qwen3-asr-0.6b', response_format='json'))


def voice_audio(value, limits):
    ledger.require(type(value) is str and 0 < len(value) <= 4*1024*1024,
                   'invalid qualified voice audio encoding')
    try:
        raw = base64.b64decode(value, validate=True)
        ledger.require(base64.b64encode(raw).decode() == value,
                       'noncanonical qualified voice audio')
        with wave.open(io.BytesIO(raw)) as source:
            count = source.getnframes()
            ledger.require(source.getnchannels() == 1 and
                source.getsampwidth() == 2 and source.getframerate() == 24000 and
                count % 1920 == 0 and 2 <= count//1920 <= limits['max_frames'],
                'qualified voice WAV geometry differs')
            data = source.readframes(count)
            ledger.require(len(data) == 2*count, 'truncated qualified voice WAV')
    except (ValueError, EOFError, wave.Error) as error:
        raise ValueError('invalid qualified voice WAV') from error
    import struct
    samples = struct.unpack('<'+'h'*count, data)
    ledger.require(max(abs(x) for x in samples) > 327 and
        sum(float(x)*x for x in samples)/count > (32768*.0001)**2,
        'silent qualified voice audio')
    return count//1920


def voice_error(value, status, code=None):
    ledger.keys(value, ('status', 'body'))
    error = value['body'].get('error') if type(value['body']) is dict else None
    ledger.require(type(value['status']) is int and value['status'] == status and
        type(error) is dict and type(error.get('code')) is str and error['code'] and
        'id' not in value['body'] and (code is None or error['code'] == code),
        'qualified voice rejection differs')


def voice_synthesis(value, fixed):
    """Shared complete output/content checks; callers bind IDs and requests."""
    profile_contract.tts_configuration(fixed, base=True)
    ledger.require(ledger.canonical(value['limits']) == ledger.canonical(fixed['limits']),
                   'qualified voice effective capacity differs')
    speech = value['speech']
    ledger.keys(speech, ('audio_base64', 'usage'))
    frames = voice_audio(speech['audio_base64'], fixed['limits'])
    usage = speech['usage']
    ledger.keys(usage, ('input_tokens', 'output_tokens', 'total_tokens'))
    ledger.require(all(ledger.integer(n, minimum=1) for n in usage.values()) and
        usage['output_tokens'] == frames*16+1 and
        usage['total_tokens'] == usage['input_tokens']+usage['output_tokens'],
        'qualified voice speech lacks natural EOS or actual usage')
    response = value['chat']
    msg = message(response, 'stop');audio = msg.get('audio')
    ledger.require(type(audio) is dict and type(audio.get('id')) is str and
        audio['id'].startswith('audio_') and ledger.integer(audio.get('expires_at'), minimum=1) and
        audio.get('transcript') == VOICE_TARGETS[1],
        'qualified registered voice Chat history differs')
    frames = voice_audio(audio.get('data'), fixed['limits'])
    usage = response.get('usage', {})
    ledger.require(type(usage) is dict, 'invalid registered voice Chat usage')
    parts = usage.get('completion_tokens_details', {})
    synthesis = usage.get('q4t_audio_synthesis', {})
    ledger.require(all(type(x) is dict for x in (usage, parts, synthesis)) and
        all(ledger.integer(usage.get(k), minimum=1) for k in
            ('prompt_tokens', 'completion_tokens', 'total_tokens')) and
        usage['total_tokens'] == usage['prompt_tokens']+usage['completion_tokens'] and
        parts.get('audio_tokens') == frames*16 and
        synthesis.get('output_tokens') == frames*16+1 and
        ledger.integer(synthesis.get('input_tokens'), minimum=1) and
        usage['completion_tokens'] > synthesis['output_tokens'] and
        usage['prompt_tokens'] > synthesis['input_tokens'],
        'qualified registered voice Chat combined usage differs')
    for key, text in [('speech_transcript', VOICE_TARGETS[0]),
                       ('chat_transcript', VOICE_TARGETS[1])]:
        ledger.require(type(value[key]) is dict and value[key].get('text') == text,
                       'qualified voice independent transcription differs')



def voice_consumer_business(value, identity):
    """Require saved-condition use with registration backends truly absent."""
    ledger.keys(value, ('requests', 'consent', 'voice', 'speech', 'chat',
        'stored_chat', 'negative', 'revocation', 'health', 'limits', 'witness'))
    ledger.require(identity.get('native_asr') is None and
        identity.get('native_tts') is None and identity.get('native_tts_base') is not None,
        'consumer receipt requires the explicit Base-only backend selection')
    ledger.require(value['witness'] == VOICE_CONSUMER_WITNESS,
                   'consumer saved-state or independent content witness differs')
    for key, field, expected in (('consent', 'language', 'en'),
                                  ('voice', 'type', 'audio_sample')):
        row = value[key]
        ledger.keys(row, ('id', 'object', 'created_at', 'name', field))
        ledger.require(row['id'] == VOICE_CONSUMER_WITNESS[key] and
            row['object'] == 'audio.'+('voice_consent' if key == 'consent' else 'voice') and
            row['name'] == ('local-consent-renamed' if key == 'consent' else 'local-sample') and
            ledger.integer(row['created_at'], minimum=1) and row[field] == expected,
            'consumer metadata changed owner state or leaked private conditions')
    expected = voice_requests(value['consent']['id'], value['voice']['id'])
    expected.pop('transcription')
    ledger.require(value['requests'] == expected, 'consumer synthesis request differs')
    for key, encoded in (('speech', value['speech']['audio_base64']),
                        ('chat', message(value['chat'], 'stop')['audio']['data'])):
        voice_audio(encoded, identity['native_tts_base']['limits'])
        raw = base64.b64decode(encoded, validate=True)
        ledger.require((len(raw), hashlib.sha256(raw).hexdigest()) ==
            VOICE_CONSUMER_AUDIO[key], 'consumer complete audio differs from recognized witness')
    # Reuse content only after matching every byte, not from the output text or
    # a prefix. There is intentionally no ASR request in this deployment.
    voice_synthesis(dict(value, speech_transcript=dict(text=VOICE_TARGETS[0]),
        chat_transcript=dict(text=VOICE_TARGETS[1])), identity['native_tts_base'])
    ledger.require(value['stored_chat'].get('id') == value['chat'].get('id') and
        message(value['stored_chat'], 'stop') == message(value['chat'], 'stop'),
        'consumer stored complete Chat audio differs')
    negatives = dict(cross_consent=(404, None), cross_speech=(404, None),
        cross_chat=(404, None), new_consent=(503, 'voice_verification_not_configured'),
        new_voice=(503, 'voice_verification_not_configured'))
    ledger.keys(value['negative'], negatives)
    for key, (status, code) in negatives.items():
        voice_error(value['negative'][key], status, code)
    revoked = value['revocation']
    ledger.keys(revoked, ('consent_deleted', 'voice_deleted', 'speech', 'chat', 'consent'))
    for key in ('consent', 'voice'):
        deleted = revoked[key+'_deleted']
        ledger.keys(deleted, ('id', 'object', 'deleted'))
        ledger.require(deleted['id'] == value[key]['id'] and
            deleted['object'] == value[key]['object'] and deleted['deleted'] is True,
            'consumer revocation did not retire the original ID')
    for key in ('speech', 'chat', 'consent'):voice_error(revoked[key], 404)
    disabled = ('tts', 'asr', 'alignment', 'vad', 'speaker', 'separator')
    ledger.keys(value['health'], ('base', 'condition', *disabled))
    for key, health in value['health'].items():
        ledger.require(type(health) is dict and health.get('reason') == 'none' and
            health.get('core_readiness_dependency') is False and
            health.get('load_failures') == 0 and health.get('compute_failures') == 0 and
            health.get('asset_cleanup_failed', False) is False,
            'consumer auxiliary health failed or affects core readiness')
        if key == 'base':
            ledger.require(health.get('model') == identity['native_tts_base']['model'] and
                health.get('state') in ('ready', 'metadata_ready') and
                ledger.integer(health.get('load_attempts'), minimum=1),
                'consumer Base synthesis was not loaded')
        else:
            ledger.require(health.get('state') == ('configured' if key == 'condition' else 'disabled') and
                all(type(health.get(k)) is int and health[k] == 0 for k in
                    ('device_bytes', 'load_attempts', 'load_failures', 'compute_failures')),
                'consumer loaded a registration-only or disabled backend')
            if key == 'condition':
                ledger.require(health.get('model') == identity['native_tts_base']['model']+'-speaker',
                               'consumer registration encoder identity differs')


def voice_icl_enabled(identity, capabilities):
    """Only actual registration requires new reference-encoding evidence.

    Base-only consumption of legacy saved vectors has its own receipt. The
    full conditioning declaration still participates in capability equality.
    """
    from tools.serve_service import package
    caps = capabilities or {}
    speech = caps.get('speech', {})
    if 'registered_voice_conditioning' not in speech:
        return False
    ledger.require(package.same_json(speech['registered_voice_conditioning'],
                                    package.VOICE_CONDITIONING),
                   'unknown registered voice conditioning contract')
    return (identity.get('native_tts_base') is not None and
            caps.get('voice_registration', {}).get('enabled') is True)


def voice_reference(records, consent_id, voice_id):
    """Validate complete persisted records, usable before/after recovery.

    This validates provenance and capacity, not acoustic similarity or native
    encoder mathematics. Those need their own retained numerical/real proofs.
    """
    from tools.serve_service import package
    ledger.keys(records, ('consent', 'voice'))
    consent, voice = records['consent'], records['voice']
    for record, kind, identifier in ((consent, 'voice_consent', consent_id),
                                      (voice, 'voice', voice_id)):
        ledger.require(type(record) is dict and record.get('kind') == kind and
            record.get('id') == identifier and type(record.get('owner')) is str and
            ledger.HASH.fullmatch(record['owner']) and
            ledger.integer(record.get('revision'), minimum=1) and
            type(record.get('expires_at')) is int and record['expires_at'] == 0 and
            type(record.get('data')) is dict,
            'persisted reference identity or lifetime differs')
    data, origin = voice['data'], consent['data']
    ledger.require(voice['owner'] == consent['owner'] and
        data.get('consent') == consent_id and
        data.get('consent_source_sha256') == origin.get('source_sha256') and
        all(type(v) is str and ledger.HASH.fullmatch(v) for v in (
            data.get('source_sha256'), origin.get('source_sha256'))) and
        type(data.get('transcript')) is str and
        0 < len(data['transcript'].encode()) <= 16384 and
        '\0' not in data['transcript'], 'persisted reference provenance differs')
    reference = data.get('reference')
    ledger.keys(reference, ('format', 'codec_sha256', 'sample_rate', 'samples', 'codes'))
    samples, codes = reference['samples'], reference['codes']
    ledger.require(reference['format'] == 'q4t.tts-reference.v1' and
        reference['codec_sha256'] == package.NATIVE_TTS_BASE['codec_weight_sha256'] and
        type(reference['sample_rate']) is int and reference['sample_rate'] == 24000 and
        ledger.integer(samples, minimum=1) and samples <= 1440000 and
        type(data.get('duration_ns')) is int and
        data['duration_ns'] == samples*1000000000//24000 and
        type(codes) is list and len(codes) == (samples+1919)//1920 and
        len(codes) <= 750 and all(type(row) is list and len(row) == 16 and
            all(ledger.integer(v) and v < 2048 for v in row) for row in codes),
        'persisted reference codes or geometry differs')
    condition = data.get('condition')
    ledger.require(type(condition) is list and len(condition) == 2048 and
        all(type(v) in (int, float) and math.isfinite(v) for v in condition) and
        0 < sum(v*v for v in condition) < math.inf and
        data.get('condition_model') == 'Qwen3-TTS-12Hz-1.7B-Base' and
        data.get('condition_revision') == package.NATIVE_TTS_BASE['revision'],
        'persisted speaker condition differs')
    return dict(samples=samples, frames=len(codes), input_codes=len(codes)*16)


def voice_icl_business(value):
    records = value['conditioning']
    checked = voice_reference(records, value['consent']['id'], value['voice']['id'])
    data, consent = records['voice']['data'], records['consent']['data']
    # Pinned real fixtures and an independent local Qwen2Tokenizer roundtrip:
    # 11 reference tokens, 7/5 target tokens, 6 structural tokens, 56*16 codes.
    # Original v9/v11 observations remain valid only for their old declaration.
    ledger.require(checked == dict(samples=107520, frames=56, input_codes=896) and
        data['source_sha256'] ==
            'a0edb0a022ae9e22d57e3a710654a2cfb13f5cea070abddf2a78b663e3b6ad88' and
        consent['source_sha256'] ==
            'f07f5bb483a614d5edc4ad29f786c58963c816c40a3d590c3aca3b5d09713dd1' and
        data['transcript'] ==
            'The meeting starts tomorrow morning. Please bring your notebook.',
        'qualified ICL fixed reference differs')
    ledger.require(value['speech']['usage']['input_tokens'] == 920 and
        value['chat']['usage']['q4t_audio_synthesis']['input_tokens'] == 918,
        'qualified ICL omitted actual reference input tokens')


def voice_business(value, identity, *, icl=False):
    """Validate retained actual results, not proof that a helper ran HTTP."""
    ledger.keys(value, ('requests', 'consent', 'voice', 'speech', 'speech_transcript',
        'chat', 'chat_transcript', 'negative', 'revocation', 'health', 'limits') +
        (('conditioning',) if icl else ()))
    fixed = identity['native_tts_base']
    profile_contract.tts_configuration(fixed, base=True)
    asr = identity.get('native_asr')
    ledger.require(type(asr) is dict and {'speaker', 'vad'} <= asr.keys(),
                   'voice registration receipt lacks qualified ASR/VAD/speaker')
    profile_contract.asr_configuration(asr, voice_registration=True)
    ledger.require(ledger.canonical(value['limits']) == ledger.canonical(fixed['limits']),
                   'qualified voice effective capacity differs')
    for name, prefix, field in [('consent', 'cons_', 'language'),
                                 ('voice', 'voice_', 'type')]:
        row = value[name]
        ledger.keys(row, ('id', 'object', 'created_at', 'name', field))
        expected = 'audio.voice_consent' if name == 'consent' else 'audio.voice'
        ledger.require(type(row['id']) is str and row['id'].startswith(prefix) and
            len(row['id']) <= 128 and len(row['id']) > len(prefix) and
            row['object'] == expected and ledger.integer(row['created_at'], minimum=1) and
            row['name'] == ('local-consent' if name == 'consent' else 'local-sample') and
            row[field] == ('en' if name == 'consent' else 'audio_sample'),
            'qualified voice metadata differs or leaks private evidence')
    expected = voice_requests(value['consent']['id'], value['voice']['id'])
    ledger.require(ledger.canonical(value['requests']) == ledger.canonical(expected),
                   'qualified voice requests lost the actual ID or supplied ASR hints')
    voice_synthesis(value, fixed)
    if icl:
        voice_icl_business(value)
    negatives = {'negated_consent': (400, 'voice_consent_mismatch'),
                 'different_speaker': (400, 'voice_speaker_mismatch'),
                 'cross_consent': (404, None), 'cross_voice': (404, None),
                 'cross_speech': (404, None), 'quota': (429, 'client_tpm_exhausted')}
    ledger.keys(value['negative'], negatives)
    for key, (status, code) in negatives.items():
        voice_error(value['negative'][key], status, code)
    revoked = value['revocation']
    ledger.keys(revoked, ('consent_deleted', 'voice_deleted', 'speech', 'consent'))
    for key in ('consent', 'voice'):
        deleted = revoked[key+'_deleted']
        ledger.keys(deleted, ('id', 'object', 'deleted'))
        ledger.require(deleted['id'] == value[key]['id'] and
            deleted['object'] == value[key]['object'] and deleted['deleted'] is True,
            'qualified voice revocation did not retire the registered ID')
    voice_error(revoked['speech'], 404);voice_error(revoked['consent'], 404)
    ledger.keys(value['health'], ('base', 'condition'))
    condition_model = fixed['model']+('-reference' if icl else '-speaker')
    for key, model in [('base', fixed['model']), ('condition', condition_model)]:
        health = value['health'][key]
        ledger.require(type(health) is dict and
            health.get('state') in ('ready', 'metadata_ready') and
            health.get('model') == model and health.get('core_readiness_dependency') is False and
            health.get('reason') == 'none' and type(health.get('compute_failures')) is int and
            health['compute_failures'] == 0,
            'qualified voice backend failed or depends on core readiness')
        if icl:
            ledger.require(health.get('asset_cleanup_pending') is False and
                type(health.get('load_failures')) is int and health['load_failures'] == 0 and
                (key != 'condition' or health.get('reference_encoder') is True),
                'qualified ICL reference backend failed or cleanup remains')


CHAT_AUDIO_FORMAT = 'q4t-profile-business-v7'
CHAT_AUDIO_SOURCE_SHA = '46dbc998c9d1d48111267c40741dd3200f2e5bcf4075f8c4c97f4451160dce50'
CHAT_AUDIO_CAPABILITY = dict(profile='q4t-chat-audio-input-v1', formats=['wav', 'mp3'],
    max_parts=8, max_source_bytes=26214400, preparation='native_asr_transcript',
    output_modalities=['text'], usage='actual_shared_preparation',
    preflight='requires_audio_preparation')


def chat_audio_capability(transcriptions):
    ledger.require(type(transcriptions) is dict, 'invalid Chat audio declaration')
    if 'chat_input' not in transcriptions:
        return False  # Retained pre-Chat releases keep their original contract.
    ledger.require(ledger.canonical(transcriptions['chat_input']) == ledger.canonical(CHAT_AUDIO_CAPABILITY),
                   'invalid Chat audio capability contract')
    return True


def chat_audio_enabled(identity, capabilities):
    if capabilities is None:
        return False
    transcriptions = capabilities.get('transcriptions', {})
    declared = chat_audio_capability(transcriptions)
    return declared and identity.get('native_asr') is not None


def chat_audio_request(encoded, *, stream=False):
    ledger.require(type(encoded) is str and 0 < len(encoded) <= 2**20,
                   'invalid qualified Chat audio source')
    try:
        source = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise ValueError('invalid qualified Chat audio encoding') from error
    ledger.require(base64.b64encode(source).decode() == encoded and
        hashlib.sha256(source).hexdigest() == CHAT_AUDIO_SOURCE_SHA,
        'qualified Chat audio source differs')
    result = dict(model='qwen3.8-flash-next', enable_thinking=False, temperature=0,
        max_completion_tokens=32, modalities=['text'], n=2, store=True,
        messages=[dict(role='user', content=[dict(type='text', text=
            '录音中被描述为几乎停滞的事情是什么？只回答对应的两个汉字，不要解释。'),
            dict(type='input_audio', input_audio=dict(data=encoded, format='wav'))])])
    if stream:
        result.update(stream=True, stream_options=dict(include_usage=True))
    return result


def chat_audio_witness(encoded, response, config, *, stream=False):
    return dict(source_sha256=CHAT_AUDIO_SOURCE_SHA,
        request_sha256=ledger.digest(chat_audio_request(encoded, stream=stream)),
        response=response, health=config['native_asr_health'],
        limits=config['native_asr_limits'])


def chat_audio_business(value, identity, encoded, *, stream=False):
    ledger.keys(value, ('source_sha256', 'request_sha256', 'response', 'health', 'limits'))
    ledger.require(value['source_sha256'] == CHAT_AUDIO_SOURCE_SHA and
        value['request_sha256'] == ledger.digest(chat_audio_request(encoded, stream=stream)),
        'qualified Chat audio request/source differs')
    fixed = identity.get('native_asr')
    ledger.require(type(fixed) is dict, 'Chat audio lacks qualified ASR')
    health = value['health']
    ledger.require(type(health) is dict and health.get('state') == 'ready' and
        health.get('reason') == 'none' and health.get('model') == fixed['model'] and
        health.get('core_readiness_dependency') is False and
        ledger.integer(health.get('compute_failures')) and health['compute_failures'] == 0 and
        ledger.integer(health.get('device_bytes'), minimum=1) and
        ledger.canonical(value['limits']) == ledger.canonical(fixed['limits']),
        'qualified Chat audio health/limits differ')
    response = value['response']
    ledger.require(type(response) is dict and response.get('object') == 'chat.completion',
                   'qualified Chat audio response missing')
    choices = response.get('choices')
    ledger.require(type(choices) is list and len(choices) == 2,
                   'qualified Chat audio choices missing')
    for index, choice in enumerate(choices):
        message = choice.get('message', {})
        ledger.require(type(choice.get('index')) is int and choice['index'] == index and
            choice.get('finish_reason') == 'stop' and message.get('role') == 'assistant' and
            message.get('content') == '交易' and not message.get('refusal') and
            not message.get('tool_calls'), 'qualified Chat audio meaning differs')
    usage = response.get('usage', {})
    prepared = usage.get('q4t_audio_preparation', {})
    details = usage.get('prompt_tokens_details', {})
    ledger.require(all(ledger.integer(usage.get(k), minimum=1) for k in
        ('prompt_tokens', 'completion_tokens', 'total_tokens')) and
        all(ledger.integer(prepared.get(k), minimum=1) for k in
            ('input_tokens', 'output_tokens', 'audio_tokens')) and
        usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'] and
        prepared['audio_tokens'] == details.get('audio_tokens') == 55 and
        prepared['audio_tokens'] < prepared['input_tokens'] < usage['prompt_tokens'] and
        prepared['output_tokens'] <= fixed['limits']['max_output_tokens'] and
        2 <= usage['completion_tokens'] - prepared['output_tokens'] <= 64 and
        ledger.integer(details.get('cached_tokens')) and
        details['cached_tokens'] <= usage['prompt_tokens'] - prepared['input_tokens'],
        'qualified Chat audio actual shared usage differs')
    return value


FORMAT = 'q4t-profile-business-v1'
MEDIA_FORMAT = 'q4t-profile-business-v2'
ASR_FORMAT = 'q4t-profile-business-v3'
AUDIO_AUXILIARY_FORMAT = 'q4t-profile-business-v4'
SPEAKER_FORMAT = 'q4t-profile-business-v5'
SEPARATOR_FORMAT = 'q4t-profile-business-v6'
SEPARATOR_FILE_SHA = 'bfa3025ba290d854b1c86539113492cd5e03d0c04b622fed28ae266caac6f001'
SEPARATOR_SAMPLES = 64000
SEPARATOR_UTTERANCES = (
    (3200, 49120, 'The child almost hurt the small dog.'),
    (3200, 35360, 'We are sure that one war is enough.'))
# Fixed SpeechBrain sample sequence: speaker 1, speaker 2, speaker 1 returns.
# Original assets/concatenation receipt: serve-speaker-http-20261118. Retain
# hashes and source intervals, not public copies of the reference audio.
SPEAKER_FILE_SHA = '4266252cab301d5f6133e49cef675fa23f549ccaa9c5907451538fd60722c51f'
SPEAKER_SAMPLES = 170080
SPEAKER_UTTERANCES = ((4800, 50720, 'A'), (66720, 98880, 'B'),
                      (114880, 165280, 'A'))
ASR_FILE_SHA = '46dbc998c9d1d48111267c40741dd3200f2e5bcf4075f8c4c97f4451160dce50'
ASR_TEXT = '甚至出现交易几乎停滞的情况。'
ASR_TEXT_IDS = [100636, 100347, 99886, 100740, 118083, 102072, 1773]
# Fixed oracle only, from the retained complete-segment business observation.
# Validation must not turn the request-local compression library into a core
# controller import dependency. Production output still measures real bytes.
ASR_TEXT_ZLIB_BYTES = 53
AUDIO_PAIR_SHA = 'a291086fad02098392160d70f61c1ba2a1be5996d442814c33897b95f96ac0bf'
MEDIA_FILE = 'tests/fixtures/file_video/transition-vp8.webm'
MEDIA_SHA = 'c1fddb4c848b966195302d193392a22856710fc6eb50640d891673f26de4b408'


def mcp_enabled(capabilities):
    return ((capabilities or {}).get('realtime', {}).get('conversation', {})
            .get('mcp', {}).get('enabled') is True)


def tracing_enabled(capabilities):
    return ((capabilities or {}).get('realtime', {}).get('conversation', {})
            .get('tracing', {}).get('enabled') is True)


def format_for(profile, identity=None, capabilities=None, business_value=None):
    if 'live' in (capabilities or {}):
        from tools.serve_recovery import profile_live_reuse
        receipts = (business_value or {}).get('live', {}).get('evidence_sha256', {})
        return profile_live_reuse.format_for(capabilities, receipts)
    if tracing_enabled(capabilities):
        return TRACING_FORMAT
    if mcp_enabled(capabilities):
        return MCP_FORMAT
    if duplex_enabled(capabilities):
        return DUPLEX_FORMAT
    if speech_enabled(capabilities):
        return SPEECH_FORMAT
    if prompt_enabled(capabilities):
        return PROMPT_FORMAT
    if conversation_enabled(capabilities):
        return CONVERSATION_FORMAT
    if semantic_vad_enabled(identity or {}, capabilities):
        return SEMANTIC_FORMAT
    if realtime_enabled(identity or {}, capabilities):
        return REALTIME_FORMAT
    if voice_icl_enabled(identity or {}, capabilities):
        return VOICE_ICL_FORMAT
    checkpoint = ((identity or {}).get('native_tts') is not None and
        tts_sampling((business_value or {}).get('tts', {})) is not None)
    if (identity or {}).get('native_tts_base') is not None:
        if (identity or {}).get('native_asr') is None and (identity or {}).get('native_tts') is None:
            return VOICE_CONSUMER_FORMAT
        return VOICE_CHECKPOINT_FORMAT if checkpoint else VOICE_FORMAT
    if (identity or {}).get('native_tts') is not None:
        return TTS_CHECKPOINT_FORMAT if checkpoint else TTS_FORMAT
    if chat_audio_enabled(identity or {}, capabilities):
        return CHAT_AUDIO_FORMAT
    if has_separator(identity or {}):
        return SEPARATOR_FORMAT
    if has_speaker(identity or {}):
        return SPEAKER_FORMAT
    if audio_auxiliaries(identity or {}):
        return AUDIO_AUXILIARY_FORMAT
    if (identity or {}).get('native_asr') is not None:
        return ASR_FORMAT
    return MEDIA_FORMAT if profile.get('allow_media', False) else FORMAT


def audio_auxiliaries(identity):
    return tuple(k for k in ('alignment', 'vad') if k in (identity.get('native_asr') or {}))


def has_speaker(identity):
    # Diarization requires alignment. A registration-only speaker is verified
    # by the voice lifecycle receipt rather than a fictitious aligned result.
    return {'speaker', 'alignment'} <= (identity.get('native_asr') or {}).keys()


def has_separator(identity):
    return 'separator' in (identity.get('native_asr') or {})


def separator_request(identity):
    ledger.require(has_separator(identity), 'missing qualified separator backend')
    return speaker_request(identity)


def separator_business(value, identity):
    """Require real mixed speech, overlapping source time and frozen quality."""
    from tools.serve_service import package
    ledger.keys(value, ('source_sha256', 'request', 'response', 'health', 'limits'))
    ledger.require(value['source_sha256'] == SEPARATOR_FILE_SHA and
        package.same_json(value['request'], separator_request(identity)),
        'qualified separator source/request differs')
    fixed, health = identity['native_asr']['separator'], value['health']
    ledger.require(type(health) is dict and health.get('model') == fixed['model'] and
        health.get('state') == 'ready' and health.get('reason') == 'none' and
        health.get('core_readiness_dependency') is False and
        ledger.integer(health.get('compute_failures')) and health['compute_failures'] == 0 and
        ledger.integer(health.get('device_bytes')) and health['device_bytes'] == 112957444 and
        package.same_json(value['limits'], fixed['limits']),
        'qualified separator health/limits differ')
    response = value['response']
    ledger.require(type(response) is dict and response.get('task') == 'transcribe' and
        response.get('duration') == SEPARATOR_SAMPLES/16000 and
        type(response.get('text')) is str and response['text'].strip(),
        'qualified separator transcript/duration missing')
    usage = response.get('usage', {})
    ledger.require(type(usage) is dict and usage.get('type') == 'tokens' and all(
        ledger.integer(usage.get(k), minimum=1) and usage[k] < 2**53 for k in
        ('input_tokens','output_tokens','total_tokens')) and
        usage['input_tokens'] + usage['output_tokens'] == usage['total_tokens'],
        'qualified separator actual token usage missing')
    details = usage.get('input_token_details', {})
    ledger.require(type(details) is dict and
        ledger.integer(details.get('audio_tokens'), minimum=1) and
        ledger.integer(details.get('text_tokens')) and
        details['audio_tokens'] + details['text_tokens'] == usage['input_tokens'],
        'qualified separator audio/text usage differs')
    segments = response.get('segments')
    ledger.require(type(segments) is list and 2 <= len(segments) <= 65536,
                   'qualified separator segments missing')
    grouped, previous = {}, 0.
    for index, row in enumerate(segments):
        ledger.require(type(row) is dict and row.get('id') == str(index) and
            row.get('type') == 'transcript.text.segment' and
            type(row.get('speaker')) is str and row['speaker'] and
            type(row.get('text')) is str and row['text'].strip() and all(
                type(row.get(k)) in (int, float) and math.isfinite(row[k])
                for k in ('start','end')),
            'qualified separator segment identity/time/text missing')
        ledger.require(previous <= row['start'] <= row['end'] <= SEPARATOR_SAMPLES/16000,
                       'qualified separator source time invalid')
        previous = row['start']
        grouped.setdefault(row['speaker'], []).append(row)
    ledger.require(len(grouped) == 2 and
        re.sub(r'\s+', '', ''.join(row['text'] for row in segments)) ==
        re.sub(r'\s+', '', response['text']), 'qualified separator lost or invented voice/text')

    def word_error(hypothesis, reference):
        # Frozen per-source WER, not substring/semantic similarity. In
        # particular hurt -> hurts is still one substitution in seven words.
        expected = re.findall(r'\w+', reference.lower())
        actual = re.findall(r'\w+', hypothesis.lower())
        costs = list(range(len(actual)+1))
        for i, word in enumerate(expected, 1):
            following = [i]
            for j, observed in enumerate(actual, 1):
                following.append(min(following[-1]+1, costs[j]+1,
                                     costs[j-1]+(word != observed)))
            costs = following
        return costs[-1]/len(expected)

    scored = []
    for labels in itertools.permutations(grouped):
        scores = [word_error(' '.join(r['text'] for r in grouped[label]), source[2])
                  for label, source in zip(labels, SEPARATOR_UTTERANCES)]
        scored.append((sum(scores), labels, scores))
    _, labels, scores = min(scored)
    # 2026-11-18 owner decision: this observed inflection preserves meaning.
    # Keep raw WER and the old failed record. This is an explicit review of
    # the pinned source, not stemming, a higher threshold, or a general waiver.
    approved = ('the child almost hurts the small dog', None)
    quality = []
    for label, source, score, allowed in zip(
            labels, SEPARATOR_UTTERANCES, scores, approved):
        observed = ' '.join(r['text'] for r in grouped[label])
        normalized = ' '.join(re.findall(r'\w+', observed.lower()))
        reviewed = score > .125 and allowed is not None and normalized == allowed
        ledger.require(score <= .125 or reviewed,
                       'qualified separator per-voice recognition quality failed')
        quality.append(dict(reference=source[2], observed=observed, wer=score,
            threshold=.125, accepted_inflection=reviewed))
    for label, (begin, end, _) in zip(labels, SEPARATOR_UTTERANCES):
        ledger.require(all(r['start']*16000 >= begin-6080 and
                           r['end']*16000 <= end+6080 for r in grouped[label]),
                       'qualified separator time outside real voice')
    overlap = max(min(a['end'],b['end'])-max(a['start'],b['start'])
                  for a in grouped[labels[0]] for b in grouped[labels[1]])
    ledger.require(overlap >= .3, 'qualified separator serialized actual overlapping speech')
    return dict(policy='source-specific-owner-inflection-20261118',
        source_sha256=SEPARATOR_FILE_SHA, voices=quality, overlap_seconds=overlap,
        model_quality_limitation=any(row['accepted_inflection'] for row in quality))


def speaker_request(identity):
    ledger.require(has_speaker(identity), 'missing qualified speaker backend')
    return dict(model=identity['native_asr']['model'], language='en',
        response_format='diarized_json', chunking_strategy=dict(type='server_vad',
            threshold=.5, prefix_padding_ms=300, silence_duration_ms=500))


def speaker_business(value, identity):
    """Require actual recurring voices, source times, usage and isolated health."""
    from tools.serve_service import package
    ledger.keys(value, ('source_sha256', 'request', 'response', 'health', 'limits'))
    ledger.require(value['source_sha256'] == SPEAKER_FILE_SHA and
        package.same_json(value['request'], speaker_request(identity)),
        'qualified speaker source/request differs')
    fixed = identity['native_asr']['speaker']
    health = value['health']
    ledger.require(type(health) is dict and health.get('model') == fixed['model'] and
        health.get('state') == 'ready' and health.get('reason') == 'none' and
        health.get('core_readiness_dependency') is False and
        ledger.integer(health.get('compute_failures')) and health['compute_failures'] == 0 and
        ledger.integer(health.get('device_bytes')) and health['device_bytes'] == 83224056 and
        package.same_json(value['limits'], fixed['limits']),
        'qualified speaker health/limits differ')
    response = value['response']
    ledger.require(type(response) is dict and response.get('task') == 'transcribe' and
        response.get('duration') == SPEAKER_SAMPLES/16000 and
        type(response.get('text')) is str and response['text'].strip(),
        'qualified speaker transcript/duration missing')
    usage = response.get('usage', {})
    ledger.require(type(usage) is dict and usage.get('type') == 'tokens' and all(
        ledger.integer(usage.get(k), minimum=1) and usage[k] < 2**53 for k in
        ('input_tokens', 'output_tokens', 'total_tokens')) and
        usage['input_tokens'] + usage['output_tokens'] == usage['total_tokens'] and
        usage['output_tokens'] <= identity['native_asr']['limits']['max_output_tokens'],
        'qualified speaker actual token usage missing')
    detail = usage.get('input_token_details', {})
    ledger.require(type(detail) is dict and
        ledger.integer(detail.get('audio_tokens'), minimum=1) and
        ledger.integer(detail.get('text_tokens')) and
        detail['audio_tokens'] + detail['text_tokens'] == usage['input_tokens'],
        'qualified speaker audio/text usage differs')
    rows = response.get('segments')
    ledger.require(type(rows) is list and 1 <= len(rows) <= 65536,
                   'qualified speaker segments missing')
    previous, seen = 0, set()
    for index, row in enumerate(rows):
        ledger.require(type(row) is dict and row.get('id') == str(index) and
            row.get('type') == 'transcript.text.segment' and
            type(row.get('text')) is str and row['text'].strip() and all(
            type(row.get(k)) in (int, float) and math.isfinite(row[k]) for k in ('start', 'end')),
            'qualified speaker segment identity/text/time missing')
        begin, end = row['start']*16000, row['end']*16000
        ledger.require(previous <= begin <= end <= SPEAKER_SAMPLES,
                       'qualified speaker source times invalid')
        previous = end
        overlaps = [max(0, min(end, b)-max(begin, a)) for a,b,_ in SPEAKER_UTTERANCES]
        owner = max(range(len(overlaps)), key=overlaps.__getitem__)
        a, b, voice = SPEAKER_UTTERANCES[owner]
        ledger.require(overlaps[owner] > 0 and row.get('speaker') == voice and
            begin >= a-6080 and end <= b+6080,
            'qualified speaker did not preserve actual recurring voices')
        seen.add(owner)
    compact = lambda s: ''.join(s.split())
    ledger.require(seen == set(range(len(SPEAKER_UTTERANCES))) and
        compact(''.join(row['text'] for row in rows)) == compact(response['text']),
        'qualified speaker dropped actual voice or transcript text')


def auxiliary_audio_source(source, identity):
    """Retain original PCM, inserting explicit silence between two utterances."""
    ledger.require(hashlib.sha256(source).hexdigest() == ASR_FILE_SHA,
                   'qualified original ASR source differs')
    if 'vad' not in audio_auxiliaries(identity):
        return source
    with wave.open(io.BytesIO(source), 'rb') as audio:
        ledger.require(audio.getnchannels() == 1 and audio.getsampwidth() == 2 and
                       audio.getframerate() == 16000, 'qualified audio shape differs')
        pcm = audio.readframes(audio.getnframes())
    target = io.BytesIO()
    with wave.open(target, 'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
        audio.writeframes(bytes(16000) + pcm + bytes(48000) + pcm + bytes(16000))
    result = target.getvalue()
    ledger.require(hashlib.sha256(result).hexdigest() == AUDIO_PAIR_SHA,
                   'qualified repeated audio differs')
    return result


def auxiliary_audio_request(identity):
    backends = audio_auxiliaries(identity)
    ledger.require(backends, 'missing qualified audio auxiliaries')
    request = dict(model=identity['native_asr']['model'])
    if 'alignment' in backends:
        request.update(response_format='verbose_json', timestamp_granularities=['word', 'segment'])
    else:
        request.update(response_format='json', include=['logprobs'])
    if 'vad' in backends:
        request['chunking_strategy'] = dict(type='server_vad', threshold=.5,
            prefix_padding_ms=300, silence_duration_ms=500)
    return request


def audio_auxiliary_business(value, identity):
    """Require real qualified optional work; a normal-ASR pass cannot replace it."""
    from tools.serve_service import package
    backends = audio_auxiliaries(identity)
    ledger.keys(value, ('source_base64', 'request', 'response', 'health', 'limits'))
    ledger.require(type(value['source_base64']) is str and len(value['source_base64']) <= 1024*1024,
                   'invalid auxiliary audio source size')
    try:
        source = base64.b64decode(value['source_base64'], validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError('invalid auxiliary audio source encoding') from error
    chunks = 2 if 'vad' in backends else 1
    seconds = 10.907 if chunks == 2 else 4.203
    ledger.require(hashlib.sha256(source).hexdigest() == (AUDIO_PAIR_SHA if chunks == 2 else ASR_FILE_SHA)
        and package.same_json(value['request'], auxiliary_audio_request(identity)),
        'qualified auxiliary audio source/request differs')
    ledger.keys(value['health'], backends)
    ledger.keys(value['limits'], backends)
    for name in backends:
        fixed = identity['native_asr'][name]
        health = value['health'][name]
        ledger.require(type(health) is dict and health.get('model') == fixed['model'] and
            health.get('state') == 'ready' and health.get('reason') == 'none' and
            health.get('core_readiness_dependency') is False and
            ledger.integer(health.get('compute_failures')) and health['compute_failures'] == 0 and
            package.same_json(value['limits'][name], fixed['limits']),
            'qualified audio auxiliary health/limits differ')
        if name == 'vad':
            ledger.require(type(health.get('device_bytes')) is int and health['device_bytes'] == 0 and
                health.get('host_weight_bytes') == 1238532, 'qualified VAD residency differs')
    response = value['response']
    ledger.require(type(response) is dict and response.get('text') == ' '.join([ASR_TEXT]*chunks),
                   'qualified auxiliary audio did not preserve actual utterances')
    if 'alignment' not in backends:
        usage = response.get('usage', {})
        ledger.require(usage.get('type') == 'tokens' and all(ledger.integer(usage.get(k), minimum=1)
            for k in ('input_tokens', 'output_tokens', 'total_tokens')) and
            usage['input_tokens']+usage['output_tokens'] == usage['total_tokens'] and
            usage['output_tokens'] <= identity['native_asr']['limits']['max_output_tokens'],
            'qualified VAD actual token usage missing')
        details = usage.get('input_token_details', {})
        ledger.require(ledger.integer(details.get('audio_tokens'), minimum=1) and
            ledger.integer(details.get('text_tokens'), minimum=1) and
            details['audio_tokens'] + details['text_tokens'] == usage['input_tokens'],
            'qualified VAD input usage differs')
        logs = response.get('logprobs')
        ledger.require(type(logs) is list and 0 < len(logs) <= usage['output_tokens'],
                       'qualified VAD actual logprobs missing')
        for row in logs:
            ledger.require(type(row) is dict and type(row.get('logprob')) in (int, float) and
                math.isfinite(row['logprob']) and row['logprob'] <= 0 and
                type(row.get('bytes')) is list and row['bytes'] and
                all(ledger.integer(b) and b <= 255 for b in row['bytes']),
                'qualified VAD probability invalid')
        ledger.require(bytes(b for row in logs for b in row['bytes']) == ASR_TEXT.encode()*chunks,
                       'qualified VAD probabilities do not cover real clip text')
        return
    ledger.require(response.get('language') == 'Chinese' and response.get('duration') == seconds and
        package.same_json(response.get('usage'), dict(type='duration', seconds=seconds)),
        'qualified aligned duration/language differs')
    words, segments = response.get('words'), response.get('segments')
    ledger.require(type(words) is list and len(words) == 13*chunks and
        all(type(v) is dict for v in words) and
        [v.get('word') for v in words] == list(ASR_TEXT[:-1])*chunks and
        type(segments) is list and len(segments) == chunks, 'qualified real word/segment count differs')
    previous = 0
    for row in words:
        ledger.require(all(type(row.get(k)) in (int, float) and math.isfinite(row[k]) for k in ('start', 'end'))
            and previous <= row['start'] <= row['end'] <= seconds, 'qualified word times differ')
        previous = row['end']
    for i, row in enumerate(segments):
        ledger.require(type(row) is dict and ledger.integer(row.get('id')) and row['id'] == i and
            ledger.integer(row.get('seek')) and row.get('text') == ASR_TEXT and
            row.get('tokens') == ASR_TEXT_IDS, 'qualified segment identities/tokens differ')
        ledger.require(all(type(row.get(k)) in (int, float) and math.isfinite(row[k]) for k in
            ('start','end','temperature','avg_logprob','compression_ratio','no_speech_prob')) and
            row['start'] == words[i*13]['start'] and row['end'] == words[(i+1)*13-1]['end'] and
            row['start'] >= row['seek']/100 and (i == 0 or row['seek'] >= 590) and
            0 <= row['temperature'] <= 1 and row['avg_logprob'] <= 0 and
            0 <= row['no_speech_prob'] < .5 and
            abs(row['compression_ratio'] - len(ASR_TEXT.encode())/ASR_TEXT_ZLIB_BYTES) < 1e-12,
            'qualified actual segment observations differ')


def audio_business(value, identity):
    """Require actual fixed audio, native text, token usage and ready state."""
    from tools.serve_service import package
    ledger.keys(value, ('source_base64', 'request', 'response', 'health', 'limits'))
    ledger.require(type(value['source_base64']) is str and
                   len(value['source_base64']) <= 1024*1024,
                   'invalid qualified ASR source size')
    try:
        raw = base64.b64decode(value['source_base64'], validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError('invalid qualified ASR source encoding') from error
    ledger.require(hashlib.sha256(raw).hexdigest() == ASR_FILE_SHA,
                   'qualified ASR source differs')
    fixed = identity['native_asr']
    ledger.require(package.same_json(value['request'], dict(model=fixed['model'],
        response_format='json', include=['logprobs'])), 'qualified ASR request differs')
    ledger.require(package.same_json(value['limits'], fixed['limits']),
                   'qualified ASR effective limits differ')
    health = value['health']
    ledger.require(type(health) is dict and health.get('model') == fixed['model'] and
        health.get('state') == 'ready' and health.get('reason') == 'none' and
        health.get('core_readiness_dependency') is False and
        ledger.integer(health.get('compute_failures')) and
        health['compute_failures'] == 0, 'qualified ASR is not healthy/isolated')
    response = value['response']
    ledger.require(type(response) is dict and response.get('text') == ASR_TEXT,
                   'qualified ASR did not transcribe actual audio')
    usage = response.get('usage', {})
    ledger.require(type(usage) is dict and usage.get('type') == 'tokens' and all(
        ledger.integer(usage.get(k), minimum=1) for k in
        ('input_tokens', 'output_tokens', 'total_tokens')) and
        usage['input_tokens']+usage['output_tokens'] == usage['total_tokens'] and
        usage['output_tokens'] <= fixed['limits']['max_output_tokens'] and
        usage['total_tokens'] <= fixed['limits']['max_context_tokens'],
        'qualified ASR actual usage missing')
    details = usage.get('input_token_details', {})
    ledger.require(type(details) is dict and
        ledger.integer(details.get('audio_tokens'), minimum=1) and
        ledger.integer(details.get('text_tokens'), minimum=1) and
        details['audio_tokens']+details['text_tokens'] == usage['input_tokens'],
        'qualified ASR input usage differs')
    logs = response.get('logprobs')
    ledger.require(type(logs) is list and 0 < len(logs) <= usage['output_tokens'],
                   'qualified ASR probabilities missing')
    for row in logs:
        ledger.require(type(row) is dict and type(row.get('token')) is str and
            type(row.get('logprob')) in (int, float) and
            math.isfinite(row['logprob']) and row['logprob'] <= 0 and
            type(row.get('bytes')) is list and 0 < len(row['bytes']) <= 4096 and
            all(ledger.integer(b) and b <= 255 for b in row['bytes']),
            'invalid qualified ASR probability')
    ledger.require(bytes(b for row in logs for b in row['bytes']) == ASR_TEXT.encode(),
                   'qualified ASR probabilities do not cover the actual text')


def media_request(data):
    """Actual unlabeled clip; the expected red/blue answer is never an input."""
    ledger.require(hashlib.sha256(data).hexdigest() == MEDIA_SHA,
                   'qualified media source differs')
    colors = ['red', 'green', 'blue', 'yellow', 'cyan', 'magenta', 'black', 'white']
    schema = dict(type='object', additionalProperties=False, required=['start','end'],
        properties={k:dict(type='string', enum=colors) for k in ['start','end']})
    return dict(model='qwen3.8-flash-next', temperature=0, max_output_tokens=128,
        reasoning=dict(effort='none'), text=dict(format=dict(type='json_schema',
            name='video_colors', strict=True, schema=schema)),
        input=[dict(role='user', content=[dict(type='input_file', filename='clip.webm',
            file_data=base64.b64encode(data).decode()), dict(type='input_text',
            text='Inspect the actual video. What is the dominant color at its start '
                 'and at its end? Return lowercase English colors in start and end.')])])


def media_business(value):
    ledger.keys(value, ('request', 'response'))
    request = value['request']
    try:
        encoded = request['input'][0]['content'][0]['file_data']
        ledger.require(type(encoded) is str and len(encoded) <= 256*1024,
                       'qualified media input size differs')
        raw = base64.b64decode(encoded, validate=True)
        ledger.require(ledger.canonical(request) == ledger.canonical(media_request(raw)),
                       'qualified media request differs')
        response = value['response']
        ledger.require(response['status'] == 'completed' and not response.get('error') and
            ledger.integer(response['usage']['input_tokens'], minimum=1),
            'qualified media completion missing')
        ledger.require(all(item.get('role') == 'assistant' for item in response['output']
            if item['type'] == 'message'), 'qualified media assistant missing')
        content = [part for item in response['output'] if item['type'] == 'message'
                   for part in item['content']]
        ledger.require(content and all(part['type'] == 'output_text' for part in content),
                       'qualified media refused or produced no text')
        answer = ''.join(part['text'] for part in content)
        ledger.require(json.loads(answer) == dict(start='red', end='blue'),
                       'qualified media did not read actual frame colors')
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError('invalid qualified media evidence') from error


def message(response, finish):
    ledger.require(type(response) is dict and type(response.get('choices')) is list and
        len(response['choices']) == 1 and response['choices'][0].get('finish_reason') == finish,
        'profile business completion missing')
    value = response['choices'][0]['message']
    ledger.require(type(value) is dict and value.get('role') == 'assistant',
                   'profile business assistant missing')
    return value


def realtime_enabled(identity, capabilities):
    declared = (capabilities or {}).get('realtime')
    if declared is None:
        return False
    ledger.require(type(declared) is dict and
        declared.get('transcription_configured') is
            (identity.get('native_asr') is not None) and
        declared.get('automatic_replay') is False and
        declared.get('per_turn_client_admission') is True,
        'Realtime registration or replay contract differs')
    return declared['transcription_configured']


def conversation_enabled(capabilities):
    declared = (capabilities or {}).get('realtime', {})
    ledger.require(type(declared) is dict, 'invalid Realtime capabilities')
    value = declared.get('conversation')
    if value is None:
        return False
    expected = dict(implemented=True, endpoint='/v1/realtime',
        profile='q4t-native-realtime-chat-v1', requires_asr=False,
        output_modalities=['text'], function_calls=True,
        per_response_client_admission=True, usage='actual_tokens',
        automatic_replay=False, out_of_band=True, cancel=True,
        persistent_context_truncation=True, max_output_tokens=4096,
        empty_context='literal_instructions_ephemeral_model_turn',
        max_responses=4, default_conversation_writers=1, max_items=128,
        history_bytes=16 << 20, session_host_reservation_bytes=2 << 30,
        pending_execution=['audio', 'mcp', 'prompt', 'tracing'])
    if type(value) is dict and value.get('profile') == 'q4t-native-realtime-chat-v2':
        from tools.serve_recovery.profile_prompt_evidence import DECLARATION
        expected.update(profile='q4t-native-realtime-chat-v2', prompt=DECLARATION,
                        pending_execution=['audio', 'mcp', 'tracing'])
        if 'speech_output' in value:
            from tools.serve_recovery.profile_speech_evidence import DECLARATION as speech
            expected.update(speech_output=speech, output_modalities=['text', 'audio'],
                            pending_execution=['audio_input', 'mcp', 'tracing'])
            if 'input_audio' in value:
                from tools.serve_recovery.profile_duplex_evidence import declaration
                configured = declared.get('transcription_configured')
                audio = declaration(configured)
                expected.update(input_audio=audio, pending_execution=['mcp','tracing'],
                    session_host_reservation_bytes=(2 << 30) +
                        (audio['additional_host_reservation_bytes'] if configured else 0))
        if 'mcp' in value:
            from tools.serve_recovery.profile_mcp_evidence import CAPABILITY
            expected.update(mcp=CAPABILITY, pending_execution=[
                item for item in expected['pending_execution'] if item != 'mcp'])
        if 'tracing' in value:
            from tools.serve_recovery.profile_trace_evidence import CAPABILITY
            expected.update(tracing=CAPABILITY, pending_execution=[
                item for item in expected['pending_execution'] if item != 'tracing'])
    ledger.require(type(value) is dict and
        ledger.canonical(value) == ledger.canonical(expected),
        'native Realtime Chat capability differs')
    return True


def prompt_enabled(capabilities):
    if not conversation_enabled(capabilities):
        return False
    return capabilities['realtime']['conversation']['profile'] == 'q4t-native-realtime-chat-v2'


def speech_enabled(capabilities):
    return (conversation_enabled(capabilities) and
            'speech_output' in capabilities['realtime']['conversation'])


def duplex_enabled(capabilities):
    return (conversation_enabled(capabilities) and
        capabilities['realtime']['conversation'].get('input_audio', {}).get(
            'configured') is True)


def conversation_output(source, session, pending_response_id=None):
    """Validate two complete source-bound tool turns, without claiming lifecycle.

    A managed fault may separately bind one known pending response. Its unknown
    usage, transport, lifetime and settlement are never inferred here.
    """
    ledger.keys(source, ('path', 'sha256', 'version'))
    ledger.require(source['path'] == 'docs/STATUS.md' and
        type(source['sha256']) is str and ledger.HASH.fullmatch(source['sha256']) and
        type(source['version']) is str and re.fullmatch(r'rev[0-9]+', source['version']),
        'actual project record identity missing')
    record = dict(record_key='release', version=source['version'],
                  source=source['path'], source_sha256=source['sha256'])
    ledger.require(session.get('tool_record') == record,
                   'actual read-only tool source differs')
    rows, events = session['responses'], session['events']
    ledger.require(type(rows) is list and len(rows) == 2 and
        all(type(row) is dict for row in rows) and
        type(events) is list and 0 < len(events) <= 4096 and
        all(type(e) is dict for e in events) and
        len(ledger.canonical(session)) <= 4 << 20 and
        not any(e.get('type') == 'error' for e in events),
        'conversation output bounds or errors')
    identifiers = [row['id'] for row in rows]
    ledger.require(all(type(i) is str and i for i in identifiers) and
        len(set(identifiers)) == 2 and
        [e['response'] for e in events if e.get('type') == 'response.done'] == rows and
        [e['response']['id'] for e in events if e.get('type') == 'response.created'] ==
            identifiers + ([pending_response_id] if pending_response_id else []),
        'conversation model terminal omitted or replayed')
    ledger.require(pending_response_id is None or
        (type(pending_response_id) is str and pending_response_id and
         pending_response_id not in identifiers), 'invalid pending response identity')
    total = 0
    for row in rows:
        ledger.require(row['status'] == 'completed' and
            type(row.get('output')) is list and len(row['output']) == 1 and
            row.get('output_modalities') == ['text'],
            'incomplete conversation model response')
        usage = row['usage']
        ledger.require(all(type(usage[k]) is int and usage[k] > 0 for k in
            ('input_tokens', 'output_tokens', 'total_tokens')) and
            usage['total_tokens'] == usage['input_tokens'] + usage['output_tokens'],
            'conversation actual model usage differs')
        total += usage['total_tokens']
    call, answer = rows[0]['output'][0], rows[1]['output'][0]
    ledger.require(all(type(item.get('id')) is str and item['id']
                       for item in (call, answer)) and call['id'] != answer['id'],
                   'conversation output item identity missing or repeated')
    ledger.require(call.get('type') == 'function_call' and
        call.get('status') == 'completed' and
        call.get('name') == 'lookup_project_record' and
        type(call.get('call_id')) is str and call['call_id'] and
        json.loads(call['arguments']) == dict(record_key='release'),
        'actual complete allowlisted function missing')
    ledger.require(answer.get('type') == 'message' and
        answer.get('role') == 'assistant' and answer.get('status') == 'completed' and
        answer.get('content') == [dict(type='output_text', text=source['version'])] and
        rows[0].get('conversation_id') == rows[1].get('conversation_id') and
        type(rows[0].get('conversation_id')) is str and rows[0]['conversation_id'],
        'source-bound answer or shared context differs')
    for row, kind, item, expected in (
        (rows[0], 'response.function_call_arguments.delta', call, call['arguments']),
        (rows[1], 'response.output_text.delta', answer, source['version'])):
        deltas = [e for e in events if e.get('type') == kind and
                  e.get('response_id') == row['id']]
        ledger.require(deltas and all(e.get('item_id') == item['id'] for e in deltas) and
            ''.join(e['delta'] for e in deltas) == expected,
            'conversation actual stream differs from terminal')
        created = next(i for i,e in enumerate(events) if
            e.get('type') == 'response.created' and e['response']['id'] == row['id'])
        done = next(i for i,e in enumerate(events) if
            e.get('type') == 'response.done' and e['response']['id'] == row['id'])
        ledger.require(all(created < i < done for i,e in enumerate(events)
                           if e in deltas), 'conversation stream event order differs')
    returned = [e['item'] for e in events if
        e.get('type') == 'conversation.item.done' and
        e.get('item', {}).get('type') == 'function_call_output']
    ledger.require(len(returned) == 1 and
        returned[0].get('call_id') == call['call_id'] and
        json.loads(returned[0]['output']) == record,
        'actual client tool return missing or unrelated')
    first_done = next(i for i,e in enumerate(events) if
        e.get('type') == 'response.done' and e['response']['id'] == identifiers[0])
    returned_at = next(i for i,e in enumerate(events) if
        e.get('type') == 'conversation.item.done' and e.get('item') == returned[0])
    second_created = next(i for i,e in enumerate(events) if
        e.get('type') == 'response.created' and e['response']['id'] == identifiers[1])
    ledger.require(first_done < returned_at < second_created,
                   'tool result did not follow actual completed call')
    return total


def conversation_business(value):
    """Two actual model turns with a source-bound read-only client tool."""
    observed = value.get('format') == 'q4t-completed-conversation-observation-v1'
    ledger.keys(value, ('source', 'session', 'before', 'after') +
                (('format',) if observed else ()))
    session = value['session']
    ledger.require(type(session) is dict and
        session.get('status') == ('PROTOCOL_PASS' if observed else 'PASS') and
        session.get('mode') == ('complete' if observed else 'qualification') and
        session.get('transport') == 'official-openai-wss' and
        type(session.get('connections')) is int and session['connections'] == 1 and
        type(session.get('close_code')) is int and session['close_code'] == 1000 and
        session.get('automatic_replay') is False and
        type(session.get('response_creates')) is int and session['response_creates'] == 2,
        'actual conversation SDK boundary differs')
    if observed:
        # This alternate actor collected lifecycle observations. Only a new
        # strict review of the actual answer below may qualify its output.
        # Wrong answers keep failing; no status or wire content is rewritten.
        ledger.require(session.get('phase') == 'result' and session.get('partial') is None and
            session.get('quality_policy') == 'record_exact_source_answer_separately' and
            session.get('model_quality') == dict(status='PASS',
                expected=value['source']['version'], actual=value['source']['version'],
                criterion='exact_source_version', response_id=session['responses'][1]['id']),
            'observed conversation has not met the exact answer criterion')
    total = conversation_output(value['source'], session)
    before, after = value['before'], value['after']
    keys = ('admitted', 'settled', 'reserved', 'active', 'sessions',
            'host_bytes', 'free_slots')
    for state in (before, after):
        ledger.keys(state, keys)
        ledger.require(all(type(state[k]) is int and state[k] >= 0 for k in keys) and
            state['free_slots'] == 1 and all(state[k] == 0 for k in
                ('reserved', 'active', 'sessions', 'host_bytes')),
            'conversation request/session resources did not drain')
    ledger.require(after['admitted'] - before['admitted'] == 2 and
        after['settled'] - before['settled'] == total,
        'conversation actual admission or settlement differs')
    return total


def semantic_vad_enabled(identity, capabilities):
    declared = (capabilities or {}).get('realtime', {}).get('semantic_vad')
    if declared is None:
        return False
    asr = identity.get('native_asr') or {}
    expected = ('vad' in asr and
                (capabilities or {}).get('structured_output', {}).get('available') is True)
    ledger.require(type(declared) is dict and
        declared.get('implemented') is True and declared.get('configured') is expected and
        declared.get('profile') == 'q4t-native-semantic-turn-v1' and
        declared.get('classifier') == 'native_primary_model' and
        declared.get('calibrated') is False and
        declared.get('usage') == 'included_in_item_and_client_tokens' and
        declared.get('serial_worker') is True and
        declared.get('maximum_wait_ms') == dict(auto=4000, low=8000, medium=4000, high=2000),
        'semantic VAD capability or configured dependencies differ')
    return expected


def semantic_settings():
    return dict(type='transcription', include=['item.input_audio_transcription.logprobs'],
        audio=dict(input=dict(format=dict(type='audio/pcm', rate=24000),
            transcription=dict(model='qwen3-asr-0.6b'), noise_reduction=None,
            turn_detection=dict(type='semantic_vad', eagerness='high'))))


def semantic_pcm(encoded):
    """Decode private receipt audio losslessly, within the existing 1 MiB bound."""
    packed = type(encoded) is dict
    if packed:
        ledger.keys(encoded, ('encoding', 'pcm_bytes', 'sha256', 'data'))
        ledger.require(encoded['encoding'] == 'zlib-base64' and
            ledger.integer(encoded['pcm_bytes'], minimum=1) and
            encoded['pcm_bytes'] <= 1 << 20 and
            type(encoded['sha256']) is str and
            re.fullmatch('[0-9a-f]{64}', encoded['sha256']) is not None,
            'semantic compressed PCM declaration differs')
    text = encoded['data'] if packed else encoded
    ledger.require(type(text) is str and len(text) <= 1 << 20,
                   'semantic retained audio encoding/size differs')
    try:
        raw = base64.b64decode(text, validate=True)
        ledger.require(base64.b64encode(raw).decode() == text,
                       'semantic noncanonical audio encoding')
        if packed:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(raw, encoded['pcm_bytes']+1)
            ledger.require(len(raw) == encoded['pcm_bytes'] and decoder.eof and
                not decoder.unused_data and not decoder.unconsumed_tail and
                hashlib.sha256(raw).hexdigest() == encoded['sha256'],
                'semantic compressed PCM length/digest differs')
    except (ValueError, TypeError, zlib.error) as error:
        raise ValueError('semantic retained audio is invalid') from error
    ledger.require(raw and len(raw) % 2 == 0, 'semantic retained PCM differs')
    return raw


def semantic_turn(value, continuation):
    ledger.require(type(value) is dict and value.get('status') == 'PASS' and
        ledger.integer(value.get('connections')) and value['connections'] == 1 and
        ledger.integer(value.get('close_code')) and value['close_code'] == 1000 and
        value.get('automatic_replay') is False, 'semantic connection not completed')
    return semantic_success_output(value, continuation)


def semantic_observed_success(value, continuation):
    """Review a collected observation against the same successful-score gate."""
    ledger.require(type(value) is dict and
        value.get('status') == 'OBSERVED_NOT_QUALIFIED' and
        value.get('observation_policy') == 'record_bounded_semantic_fallback_v1' and
        value.get('transport') == 'openai-realtime-wss' and
        type(value.get('connections')) is int and value['connections'] == 1 and
        type(value.get('close_code')) is int and value['close_code'] == 1000 and
        value.get('automatic_replay') is False, 'observed semantic lifecycle differs')
    return semantic_success_output(value, continuation)


def semantic_success_output(value, continuation):
    # Both actors must have an actual accepted classifier, early completion,
    # exact transcript, usage, retained audio and correlated events. A bounded
    # unknown-probability fallback is never accepted by this helper.
    terminal = value['terminal']; use = terminal['usage']
    ledger.require(type(terminal.get('item_id')) is str and 0 < len(terminal['item_id']) <= 128 and
        terminal.get('transcript') == ASR_TEXT and value.get('delta') == ASR_TEXT and
        use.get('type') == 'tokens' and all(ledger.integer(use.get(k), minimum=1)
        for k in ('input_tokens', 'output_tokens', 'total_tokens')) and
        use['input_tokens']+use['output_tokens'] == use['total_tokens'],
        'semantic actual transcript or usage differs')
    details = use['input_token_details']
    ledger.require(all(ledger.integer(details.get(k), minimum=1)
        for k in ('audio_tokens', 'text_tokens')) and
        details['audio_tokens']+details['text_tokens'] == use['input_tokens'],
        'semantic input usage differs')
    observed = terminal['q4t_semantic_vad']
    counters = ('attempts', 'accepted', 'unavailable', 'invalid', 'stale',
                'input_tokens', 'output_tokens')
    ledger.require(all(ledger.integer(observed.get(k)) for k in counters) and
        observed.get('profile') == 'q4t-native-semantic-turn-v1' and
        0 < observed['accepted'] <= observed['attempts'] <= 8 and
        sum(observed[k] for k in counters[1:5]) == observed['attempts'] and
        observed.get('maximum_wait') is False and observed.get('probability_available') is True and
        observed.get('included_in_usage') is True and
        0 < observed['input_tokens'] < use['input_tokens'] and
        0 < observed['output_tokens'] < use['output_tokens'] and observed['output_tokens'] <= 128,
        'semantic actual classifier or included work missing')
    logs = terminal['logprobs']
    ledger.require(type(logs) is list and 0 < len(logs) <= use['output_tokens'],
        'semantic actual logprobs missing')
    for row in logs:
        ledger.require(type(row.get('token')) is str and
            type(row.get('logprob')) in (int, float) and math.isfinite(row['logprob']) and
            row['logprob'] <= 0 and type(row.get('bytes')) is list and row['bytes'] and
            all(ledger.integer(b) and b <= 255 for b in row['bytes']),
            'semantic logprob malformed')
    ledger.require(bytes(b for row in logs for b in row['bytes']) == ASR_TEXT.encode(),
        'semantic logprobs do not cover the actual transcript')
    events = value['events']
    ledger.require(not any(e['type'] in ('error',
        'conversation.item.input_audio_transcription.failed') for e in events),
        'semantic completion cannot hide a failure')
    for kind in ('input_audio_buffer.speech_started', 'input_audio_buffer.speech_stopped',
                 'input_audio_buffer.committed', 'conversation.item.input_audio_transcription.completed'):
        rows = [e for e in events if e['type'] == kind]
        ledger.require(len(rows) == 1 and rows[0]['item_id'] == terminal['item_id'],
            'semantic item event correlation differs')
    pause = value.get('continuation')
    if continuation:
        ledger.require(type(pause) is dict and pause.get('no_commit') is True and
            pause.get('original_samples_sent') == 36000 and pause.get('pause_samples') == 12000 and
            pause.get('total_original_samples') == 100895,
            'semantic unfinished prefix was not preserved')
    else:
        ledger.require(pause is None, 'unexpected semantic continuation')
    item = value['retrieved']['item']; content = item['content']
    ledger.require(item['id'] == terminal['item_id'] and len(content) == 1 and
        content[0]['transcript'] == ASR_TEXT and
        ledger.integer(value.get('sent_samples'), minimum=1),
        'semantic retained audio identity/size differs')
    raw = semantic_pcm(content[0]['audio'])
    ledger.require(len(raw) <= value['sent_samples']*2, 'semantic retained PCM differs')
    return use['total_tokens']


def semantic_business(value, identity):
    from tools.serve_service import package
    observed = value.get('format') == 'q4t-completed-semantic-observation-v1'
    ledger.keys(value, ('input_sha256', 'settings', 'turns', 'before', 'after',
        'asr_health', 'vad_health', 'asr_limits', 'vad_limits') +
        (('format',) if observed else ()))
    ledger.require(value['input_sha256'] == REALTIME_INPUT_SHA and
        package.same_json(value['settings'], semantic_settings()) and
        type(value['turns']) is list and len(value['turns']) == 2,
        'semantic actual source/settings/turns differ')
    verify = semantic_observed_success if observed else semantic_turn
    tokens = sum(verify(turn, i == 1) for i, turn in enumerate(value['turns']))
    for name, fixed in (('asr', identity['native_asr']),
                        ('vad', identity['native_asr']['vad'])):
        health = value[name+'_health']
        ledger.require(health.get('state') == 'ready' and health.get('reason') == 'none' and
            health.get('model') == fixed['model'] and health.get('core_readiness_dependency') is False and
            ledger.integer(health.get('compute_failures')) and health['compute_failures'] == 0 and
            package.same_json(value[name+'_limits'], fixed['limits']),
            'semantic backend health or effective limits differ')
    for state in (value['before'], value['after']):
        ledger.keys(state, ('admitted', 'settled', 'reserved', 'active', 'sessions',
                            'host_bytes', 'free_slots'))
        ledger.require(all(ledger.integer(n) for n in state.values()) and
            all(state[k] == 0 for k in ('reserved', 'active', 'sessions', 'host_bytes')) and
            state['free_slots'] > 0, 'semantic resources not drained')
    before, after = value['before'], value['after']
    ledger.require(after['admitted']-before['admitted'] == 2 and
        after['settled']-before['settled'] == tokens and after['free_slots'] == before['free_slots'],
        'semantic real client settlement differs')


def realtime_business(value):
    """Require real history-conditioned usage, exact waveform and settlement.

    The whole original SDK stream is retained beside this compact projection.
    Existing independent tokenizer/processed-waveform witnesses are immutable;
    a different engine still needs its own observed session on each profile.
    """
    shared = 'waveform' in value
    ledger.keys(value, ('input_sha256', 'session', 'before', 'after') +
                (('waveform',) if shared else ()))
    ledger.require(value['input_sha256'] == REALTIME_INPUT_SHA,
                   'Realtime fixed input differs')
    session = value['session']
    ledger.keys(session, ('status', 'connections', 'close_code', 'automatic_replay',
                         'turns', 'checks'))
    ledger.require(session['status'] == 'PASS' and
        type(session['connections']) is int and session['connections'] == 1 and
        session['close_code'] == 1000 and session['automatic_replay'] is False and
        type(session['turns']) is list and len(session['turns']) == 3,
        'Realtime session failed or replayed')
    identifiers = []
    for i, turn in enumerate(session['turns']):
        identifier = turn['item_id']; identifiers.append(identifier)
        terminal, committed = turn['terminal'], turn['committed']
        item = turn['retrieved']['item']
        ledger.require(type(identifier) is str and identifier and
            terminal.get('item_id') == committed.get('item_id') == item.get('id') == identifier and
            terminal.get('type') == 'conversation.item.input_audio_transcription.completed' and
            turn['retrieved'].get('type') == 'conversation.item.retrieved' and
            committed.get('type') == 'input_audio_buffer.committed' and
            committed.get('previous_item_id') == (identifiers[0] if i == 1 else None) and
            turn.get('before_commit') is None and
            terminal.get('transcript') == turn.get('delta') == ASR_TEXT and
            item.get('object') == 'realtime.item' and item.get('type') == 'message' and
            item.get('role') == 'user' and item.get('status') == 'completed' and
            type(item.get('content')) is list and len(item['content']) == 1,
            'Realtime actual completion/history identity differs')
        content = item['content'][0]
        ledger.keys(content, ('type', 'transcript', 'audio'))
        encoded = content['audio']
        if shared:
            ledger.require(encoded == dict(sha256=REALTIME_WAVEFORM_SHA),
                           'Realtime waveform reference missing or changed')
            encoded = value['waveform']
        ledger.require(content['type'] == 'input_audio' and content['transcript'] == ASR_TEXT and
            type(encoded) is str and len(encoded) <= 1 << 20,
            'Realtime retained transcript/format differs')
        try:
            audio = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ValueError('invalid Realtime retained audio encoding') from error
        ledger.require(base64.b64encode(audio).decode() == encoded and
            len(audio) == 201788 and hashlib.sha256(audio).hexdigest() == REALTIME_WAVEFORM_SHA,
            'Realtime retrieved waveform differs from actual processing')
        usage = terminal.get('usage', {})
        initial = 81 if i == 1 else 70
        ledger.require(type(usage) is dict and usage.get('type') == 'tokens' and
            all(type(usage.get(k)) is int for k in
                ('input_tokens', 'output_tokens', 'total_tokens')) and
            usage['input_tokens'] == initial and usage['output_tokens'] == 11 and
            usage['total_tokens'] == initial+11,
            'Realtime actual history/deletion input or output usage differs')
    ledger.require(len(set(identifiers)) == 3 and type(session['checks']) is list and
                   len(session['checks']) == 4, 'Realtime item/delete count differs')
    for i, check in enumerate(session['checks']):
        identifier = identifiers[i//2]; event = check['event']
        ledger.require(check['item_id'] == identifier and
            check['type'] == ('deleted' if i % 2 == 0 else 'absent'),
            'Realtime deletion identity differs')
        if i % 2 == 0:
            ledger.require(event.get('type') == 'conversation.item.deleted' and
                event.get('item_id') == identifier, 'Realtime delete acknowledgement differs')
        else:
            ledger.require(event.get('type') == 'error' and
                event.get('error', {}).get('code') == 'item_not_found' and
                event['error'].get('event_id') == check.get('label') and
                type(check.get('label')) is str and check['label'],
                'Realtime deleted-item lookup/correlation differs')
    for side in ('before', 'after'):
        state = value[side]
        ledger.keys(state, ('admitted', 'settled', 'reserved', 'active', 'sessions',
                            'host_bytes', 'free_slots'))
        ledger.require(all(ledger.integer(v) for v in state.values()) and
            all(state[k] == 0 for k in ('reserved', 'active', 'sessions', 'host_bytes')) and
            state['free_slots'] == 1, 'Realtime session/admission resources not released')
    ledger.require(value['after']['admitted']-value['before']['admitted'] == 3 and
        value['after']['settled']-value['before']['settled'] == 254,
        'Realtime actual rate settlement differs')


def business(value, identity, selected=None, capabilities=None):
    media = (selected or {}).get('allow_media', False)
    chat_audio = chat_audio_enabled(identity, capabilities)
    ledger.keys(value, ('schema', 'tool_call', 'tool_result', 'tool_reply',
                       'tool_source', 'embedding') + (('media',) if media else ()) +
                       (('asr',) if identity.get('native_asr') is not None else ()) +
                       (('asr_auxiliary',) if audio_auxiliaries(identity) else ()) +
                       (('speaker',) if has_speaker(identity) else ()) +
                       (('separator',) if has_separator(identity) else ()) +
                       (('chat_audio',) if chat_audio else ()) +
                       (('tts',) if identity.get('native_tts') is not None else ()) +
                       (('voice',) if identity.get('native_tts_base') is not None else ()) +
                       (('realtime',) if realtime_enabled(identity, capabilities) else ()) +
                       (('semantic_vad',) if semantic_vad_enabled(identity, capabilities) else ()) +
                       (('conversation',) if conversation_enabled(capabilities) else ()) +
                       (('prompt',) if prompt_enabled(capabilities) else ()) +
                       (('realtime_speech',) if speech_enabled(capabilities) else ()) +
                       (('realtime_duplex',) if duplex_enabled(capabilities) else ()) +
                       (('realtime_mcp',) if mcp_enabled(capabilities) else ()) +
                       (('realtime_tracing',) if tracing_enabled(capabilities) else ()) +
                       (('live',) if 'live' in (capabilities or {}) else ()))
    if tracing_enabled(capabilities):
        from tools.serve_recovery import profile_trace_evidence as tracing
        tracing.business(value['realtime_tracing'])
    if mcp_enabled(capabilities):
        from tools.serve_recovery import profile_mcp_evidence as mcp
        ledger.require(ledger.canonical(capabilities['realtime']['conversation']['mcp']) ==
                       ledger.canonical(mcp.CAPABILITY),
                       'qualified MCP capability differs')
        mcp.business(value['realtime_mcp'])
    if duplex_enabled(capabilities):
        from tools.serve_recovery.profile_duplex_evidence import business as duplex_business
        duplex_business(value['realtime_duplex'])
    if speech_enabled(capabilities):
        from tools.serve_recovery.profile_speech_evidence import business as speech_business
        speech_business(value['realtime_speech'])
    if prompt_enabled(capabilities):
        from tools.serve_recovery.profile_prompt_evidence import business as prompt_business
        prompt_business(value['prompt'])
    if conversation_enabled(capabilities):
        conversation_business(value['conversation'])
    if realtime_enabled(identity, capabilities):
        realtime_business(value['realtime'])
    if semantic_vad_enabled(identity, capabilities):
        semantic_business(value['semantic_vad'], identity)
    if identity.get('native_tts') is not None:
        tts_business(value['tts'], identity)
    if identity.get('native_tts_base') is not None:
        if identity.get('native_asr') is None and identity.get('native_tts') is None:
            voice_consumer_business(value['voice'], identity)
        else:
            voice_business(value['voice'], identity,
                           icl=voice_icl_enabled(identity, capabilities))
    if media:
        media_business(value['media'])
    if identity.get('native_asr') is not None:
        audio_business(value['asr'], identity)
    if chat_audio:
        chat_audio_business(value['chat_audio'], identity,
                                    value['asr']['source_base64'])
    if audio_auxiliaries(identity):
        audio_auxiliary_business(value['asr_auxiliary'], identity)
    if has_speaker(identity):
        speaker_business(value['speaker'], identity)
    if has_separator(identity):
        separator_business(value['separator'], identity)
    schema = message(value['schema'], 'stop')
    ledger.require(json.loads(schema['content']) == {'answer': 42},
                   'profile structured business differs')
    source = value['tool_source']
    ledger.keys(source, ('path', 'content'))
    ledger.require(source['path'] == 'docs/SERVE_MEMORY_STOP_2026-11-03.md' and
        type(source['content']) is str and 0 < len(source['content'].encode()) <= 128*1024,
        'profile real project source missing')
    digest = hashlib.sha256(source['content'].encode()).hexdigest()
    calls = message(value['tool_call'], 'tool_calls').get('tool_calls')
    ledger.require(type(calls) is list and len(calls) == 1 and
        type(calls[0].get('id')) is str and 1 <= len(calls[0]['id']) <= 256 and
        calls[0].get('type') == 'function' and
        calls[0]['function']['name'] == 'project_document' and
        json.loads(calls[0]['function']['arguments']) == {'path': source['path']},
        'profile tool call differs')
    reply = value['tool_reply']
    ledger.keys(reply, ('role', 'tool_call_id', 'content'))
    ledger.require(reply['role'] == 'tool' and reply['tool_call_id'] == calls[0]['id'] and
        json.loads(reply['content']) == dict(path=source['path'], sha256=digest,
                                           answer=digest[:12]),
        'profile tool reply is not derived from the retained project source')
    result = message(value['tool_result'], 'stop')
    ledger.require(type(result.get('content')) is str and digest[:12] in result['content'] and
                   not result.get('tool_calls'), 'profile tool loop did not consume actual result')
    embedding = value['embedding']
    if identity['native_embedding'] is None:
        ledger.require(embedding is None, 'unexpected qualified embedding backend')
    else:
        ledger.require(type(embedding) is dict and embedding.get('object') == 'list' and
            embedding.get('model') == identity['native_embedding']['model'] and
            len(embedding.get('data', [])) == 1 and
            embedding['data'][0].get('index') == 0,
            'qualified dedicated embedding business missing')
        vector = embedding['data'][0].get('embedding')
        ledger.require(type(vector) is list and len(vector) == 32 and all(
            type(x) in (int, float) and math.isfinite(x) for x in vector) and
            abs(sum(x*x for x in vector)-1) < .001,
            'qualified embedding shape or norm differs')


# A version registration can contain copies of the same profile in the root,
# selected model and fallback. Share only the pure historical proof check.
# All file, registry, ingress, current observation and asset checks still run.
_verified_live_records = ContextVar('verified_live_records', default=None)


@contextmanager
def qualification_scope():
    token = _verified_live_records.set(set())
    try:
        yield
    finally:
        _verified_live_records.reset(token)


def _validate_live_record(value):
    from tools.serve_recovery import profile_live_reuse
    seen = _verified_live_records.get()
    if seen is None:
        profile_live_reuse.validate(value)
        return
    # Hash the actual parsed record, including its history. A declared digest
    # or a filesystem path cannot establish equality with an earlier record.
    digest = profile_live_reuse.record_hash(value)
    if digest not in seen:
        profile_live_reuse.validate(value)
        # Two capacities in each of the root, primary and fallback packages.
        if len(seen) < 6:
            seen.add(digest)


def validate(value, registry, name, digest):
    ledger.keys(value, ('format', 'profile', 'identity', 'capacity', 'capabilities',
        'configuration', 'startup_memory', 'business', 'environment') +
        (('reused_business',) if 'reused_business' in value else ()))
    profile_contract.validate(registry)
    ledger.require(value['format'] == format_for(registry['profiles'][name],
                                                registry['identity'], value['capabilities'],
                                                value['business']) and
        value['profile'] == name and
        value['identity'] == registry['identity'] and
        value['capacity'] == registry['profiles'][name], 'profile evidence identity differs')
    actual = profile_contract.qualification(name, value['capacity'], value['identity'],
        value['capabilities'], value['configuration'], value['startup_memory'], digest)
    ledger.require(actual == registry['qualifications'][name],
                   'profile receipt does not match retained observations')
    observed = value['business']
    if value['format'] in ('q4t-profile-business-v26', 'q4t-profile-business-v27',
                           'q4t-profile-business-v28', 'q4t-profile-business-v29',
                           'q4t-profile-business-v30', 'q4t-profile-business-v31',
                           'q4t-profile-business-v32', 'q4t-profile-business-v33', 'q4t-profile-business-v34',
                           'q4t-profile-business-v35', 'q4t-profile-business-v36',
                           'q4t-profile-business-v37', 'q4t-profile-business-v38',
                           'q4t-profile-business-v39', 'q4t-profile-business-v40',
                           'q4t-profile-business-v41', 'q4t-profile-business-v42', 'q4t-profile-business-v43', 'q4t-profile-business-v44',
                           'q4t-profile-business-v45', 'q4t-profile-business-v46', 'q4t-profile-business-v47',
                           'q4t-profile-business-v48', 'q4t-profile-business-v49', 'q4t-profile-business-v50',
                           'q4t-profile-business-v51', 'q4t-profile-business-v52', 'q4t-profile-business-v53', 'q4t-profile-business-v54', 'q4t-profile-business-v55',
                           'q4t-profile-business-v56',
                           'q4t-profile-business-v57'):
        from tools.serve_recovery import profile_duplex_reuse
        observed = dict(observed, realtime_duplex=profile_duplex_reuse.unpack_text(
            observed['realtime_duplex']))
    business(observed, value['identity'], value['capacity'], value['capabilities'])
    if 'live' in value['capabilities']:
        ledger.require('reused_business' in value, 'Live business source missing')
        _validate_live_record(value)
    elif 'reused_business' in value:
        if value['reused_business'].get('format') == 'q4t-tracing-profile-source-reuse-v1':
            from tools.serve_recovery import profile_trace_reuse as verifier
        elif value['reused_business'].get('format') == 'q4t-mcp-profile-source-reuse-v1':
            from tools.serve_recovery import profile_mcp_reuse as verifier
        elif value['reused_business'].get('format') == 'q4t-duplex-composed-source-reuse-v1':
            from tools.serve_recovery import profile_duplex_composed_reuse as verifier
        elif value['reused_business'].get('format') == 'q4t-duplex-source-reuse-v1':
            from tools.serve_recovery import profile_duplex_reuse as verifier
        elif value['reused_business'].get('format') in (
                'q4t-speech-source-reuse-v1',
                'q4t-speech-cancellation-source-reuse-v1'):
            from tools.serve_recovery import profile_speech_reuse as verifier
        elif value['reused_business'].get('format') in (
                'q4t-prompt-refusal-source-reuse-v1',
                'q4t-prompt-refusal-source-assembly-v1'):
            from tools.serve_recovery import profile_refusal_reuse as verifier
        else:
            from tools.serve_recovery import profile_prompt_reuse as verifier
        verifier.validate(value)
    environment = value['environment']
    ledger.keys(environment, ('status', 'env_abort', 'all_queries_ok',
        'nvml_queries', 'lease_result_sha256', 'cleanup_complete'))
    ledger.require(environment['status'] == 'PASS' and environment['env_abort'] is False and
        environment['all_queries_ok'] is True and environment['cleanup_complete'] is True and
        ledger.integer(environment['nvml_queries'], minimum=1) and
        type(environment['lease_result_sha256']) is str and
        ledger.HASH.fullmatch(environment['lease_result_sha256']),
        'profile environment or cleanup was not accepted')
    return value
