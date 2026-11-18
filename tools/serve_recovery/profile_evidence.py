"""Verify retained profile observations and actual business result contracts."""
import base64
import hashlib
import io
import itertools
import json
import math
import re
import wave

from tools.serve_recovery import ledger, profile_contract

TTS_FORMAT = 'q4t-profile-business-v8'
VOICE_FORMAT = 'q4t-profile-business-v9'
TTS_CHECKPOINT_FORMAT = 'q4t-profile-business-v10'
VOICE_CHECKPOINT_FORMAT = 'q4t-profile-business-v11'
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


def voice_business(value, identity):
    """Validate retained actual results, not proof that a helper ran HTTP."""
    ledger.keys(value, ('requests', 'consent', 'voice', 'speech', 'speech_transcript',
        'chat', 'chat_transcript', 'negative', 'revocation', 'health', 'limits'))
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
    for key, model in [('base', fixed['model']), ('condition', fixed['model']+'-speaker')]:
        health = value['health'][key]
        ledger.require(type(health) is dict and
            health.get('state') in ('ready', 'metadata_ready') and
            health.get('model') == model and health.get('core_readiness_dependency') is False and
            health.get('reason') == 'none' and type(health.get('compute_failures')) is int and
            health['compute_failures'] == 0,
            'qualified voice backend failed or depends on core readiness')


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


def format_for(profile, identity=None, capabilities=None, business_value=None):
    checkpoint = ((identity or {}).get('native_tts') is not None and
        tts_sampling((business_value or {}).get('tts', {})) is not None)
    if (identity or {}).get('native_tts_base') is not None:
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
                       (('voice',) if identity.get('native_tts_base') is not None else ()))
    if identity.get('native_tts') is not None:
        tts_business(value['tts'], identity)
    if identity.get('native_tts_base') is not None:
        voice_business(value['voice'], identity)
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


def validate(value, registry, name, digest):
    ledger.keys(value, ('format', 'profile', 'identity', 'capacity', 'capabilities',
        'configuration', 'startup_memory', 'business', 'environment'))
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
    business(value['business'], value['identity'], value['capacity'], value['capabilities'])
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
