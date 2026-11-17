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


def format_for(profile, identity=None):
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
    return 'speaker' in (identity.get('native_asr') or {})


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
    ledger.require(all(v <= .125 for v in scores),
                   'qualified separator per-voice recognition quality failed')
    for label, (begin, end, _) in zip(labels, SEPARATOR_UTTERANCES):
        ledger.require(all(r['start']*16000 >= begin-6080 and
                           r['end']*16000 <= end+6080 for r in grouped[label]),
                       'qualified separator time outside real voice')
    overlap = max(min(a['end'],b['end'])-max(a['start'],b['start'])
                  for a in grouped[labels[0]] for b in grouped[labels[1]])
    ledger.require(overlap >= .3, 'qualified separator serialized actual overlapping speech')


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


def business(value, identity, selected=None):
    media = (selected or {}).get('allow_media', False)
    ledger.keys(value, ('schema', 'tool_call', 'tool_result', 'tool_reply',
                       'tool_source', 'embedding') + (('media',) if media else ()) +
                       (('asr',) if identity.get('native_asr') is not None else ()) +
                       (('asr_auxiliary',) if audio_auxiliaries(identity) else ()) +
                       (('speaker',) if has_speaker(identity) else ()) +
                       (('separator',) if has_separator(identity) else ()))
    if media:
        media_business(value['media'])
    if identity.get('native_asr') is not None:
        audio_business(value['asr'], identity)
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
                                                registry['identity']) and
        value['profile'] == name and
        value['identity'] == registry['identity'] and
        value['capacity'] == registry['profiles'][name], 'profile evidence identity differs')
    actual = profile_contract.qualification(name, value['capacity'], value['identity'],
        value['capabilities'], value['configuration'], value['startup_memory'], digest)
    ledger.require(actual == registry['qualifications'][name],
                   'profile receipt does not match retained observations')
    business(value['business'], value['identity'], value['capacity'])
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
