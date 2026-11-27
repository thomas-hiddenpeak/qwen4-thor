"""Native ASR multipart, finite output and typed streaming declarations."""
import hashlib
from pathlib import Path

SDK_FILE = 'src/resources/audio/transcriptions.ts'
SDK_SOURCES = {SDK_FILE:
    '8a81d4cb4c975cf027f78b8d86feb2fb39e7860b265433a91ebf13bef9f61d80',
    'src/resources/audio/translations.ts':
    '1868adba37b8cc402975ad3a51f23ae506520abf4bdfb5314b9ed7656bbc404d'}


def definitions():
    from tools.serve_responses.build_contract import obj, ref, nullable
    text = dict(type='string')
    count = dict(type='integer', minimum=0)
    strings = lambda maximum: dict(type='array', maxItems=maximum,
        uniqueItems=True, items=dict(type='string', minLength=1))
    request = obj(dict(file=dict(type='string', format='binary'),
        model=dict(type='string', minLength=1), language=dict(type='string', minLength=2),
        prompt=dict(type='string', **{'x-q4t-max-utf8-bytes':65536}),
        temperature=dict(type='number', minimum=0, maximum=1),
        stream=nullable(dict(type='boolean')),
        response_format=dict(enum=['json', 'text', 'srt', 'verbose_json', 'vtt', 'diarized_json']),
        include=dict(type='array', maxItems=1, uniqueItems=True,
                     items=dict(const='logprobs')),
        languages=strings(64), keywords=strings(64),
        timestamp_granularities=dict(type='array', maxItems=2, uniqueItems=True,
                                     items=dict(enum=['word', 'segment'])),
        known_speaker_names=strings(4), known_speaker_references=strings(4),
        chunking_strategy=dict(anyOf=[dict(const='auto'), obj(dict(
            type=dict(const='server_vad'), threshold=dict(type='number', minimum=0, maximum=1),
            prefix_padding_ms=dict(type='integer', minimum=0, maximum=30000),
            silence_duration_ms=dict(type='integer', minimum=0, maximum=30000)), ['type'])])),
        ['file', 'model'])
    request['allOf'] = [
        {'if':{'required':['include'], 'properties':{'include':{'minItems':1}}},
         'then':{'properties':{'response_format':{'const':'json'}}}},
        {'if':{'required':['timestamp_granularities'],
               'properties':{'timestamp_granularities':{'minItems':1}}},
         'then':{'required':['response_format'],
                 'properties':{'response_format':{'const':'verbose_json'}}}},
        {'if':{'required':['stream'], 'properties':{'stream':{'const':True}}},
         'then':{'properties':{'response_format':{'enum':['json', 'text', 'diarized_json']}}}}]
    probability = obj(dict(token=text, logprob=dict(type='number', maximum=0),
        bytes=dict(type='array', minItems=1,
                   items=dict(type='integer', minimum=0, maximum=255))),
        ['token', 'logprob', 'bytes'])
    probabilities = dict(type='array', items=ref('transcription_logprob'))
    usage = obj(dict(type=dict(const='tokens'), input_tokens=count,
        output_tokens=count, total_tokens=count,
        input_token_details=obj(dict(audio_tokens=count, text_tokens=count),
                                ['audio_tokens', 'text_tokens'])),
        ['type', 'input_tokens', 'output_tokens', 'total_tokens', 'input_token_details'])
    response_fields = dict(text=text, logprobs=probabilities, usage=usage,
        languages=dict(type='array', items=obj(dict(code=text), ['code'])))
    response = obj(response_fields, ['text', 'usage'])
    delta = obj(dict(type=dict(const='transcript.text.delta'), delta=text,
                     logprobs=probabilities,
                     segment_id=dict(type='string', minLength=1, maxLength=32)),
                ['type', 'delta'])
    done = obj(dict(response_fields, type=dict(const='transcript.text.done')),
               ['type', 'text', 'usage'])
    error = obj(dict(type=dict(const='error'), code=text, message=text,
                     param=nullable(text)), ['type', 'code', 'message', 'param'])
    seconds = dict(type='number', minimum=0, maximum=600)
    diarized_segment = obj(dict(id=dict(type='string', minLength=1, maxLength=32),
        type=dict(const='transcript.text.segment'), start=seconds, end=seconds,
        speaker=dict(type='string', minLength=1, maxLength=256), text=text),
        ['id', 'type', 'start', 'end', 'speaker', 'text'])
    diarized_fields = dict(task=dict(const='transcribe'), duration=seconds,
        segments=dict(type='array', maxItems=65536,
                      items=ref('transcription_diarized_segment')))
    diarized = obj(dict(diarized_fields, text=text, usage=usage),
                    ['task', 'duration', 'segments', 'text', 'usage'])
    done['properties'].update(diarized_fields)
    done['allOf'] = [dict(
        **{'if':dict(anyOf=[dict(required=[key]) for key in diarized_fields]),
           'then':dict(required=list(diarized_fields))})]
    word = obj(dict(word=text, start=seconds, end=seconds), ['word', 'start', 'end'])
    segment = obj(dict(id=count, seek=count, start=seconds, end=seconds, text=text,
        tokens=dict(type='array', minItems=1, items=dict(type='integer', minimum=0, maximum=151935)),
        temperature=dict(type='number', minimum=0, maximum=1),
        avg_logprob=dict(type='number', maximum=0),
        compression_ratio=dict(type='number', minimum=0),
        no_speech_prob=dict(type='number', minimum=0, maximum=1)),
        ['id', 'seek', 'start', 'end', 'text', 'tokens', 'temperature',
         'avg_logprob', 'compression_ratio', 'no_speech_prob'])
    verbose = obj(dict(text=text, language=text, duration=seconds,
        words=dict(type='array', items=ref('transcription_word')),
        segments=dict(type='array', items=ref('transcription_segment')),
        usage=obj(dict(type=dict(const='duration'), seconds=seconds), ['type', 'seconds'])),
        ['text', 'language', 'duration', 'usage'])
    verbose['anyOf'] = [dict(required=['words']), dict(required=['segments'])]
    translation = obj(dict(file=request['properties']['file'], model=request['properties']['model'],
        prompt=request['properties']['prompt'], temperature=request['properties']['temperature'],
        response_format=dict(enum=['json','text','srt','verbose_json','vtt'])), ['file','model'])
    return dict(translation_create=translation,
        translation_response=obj(dict(text=text), ['text']),
        translation_verbose_response=obj(dict(text=text, language=dict(const='english'),
            duration=seconds), ['text','language','duration']),
        transcription_create=request, transcription_response=response,
        transcription_verbose_response=verbose, transcription_word=word,
        transcription_segment=segment,
        transcription_diarized_response=diarized,
        transcription_diarized_segment=diarized_segment,
        transcription_logprob=probability, transcription_delta=delta,
        transcription_done=done, transcription_error=error,
        transcription_event=dict(anyOf=[ref('transcription_delta'),
            ref('transcription_done'), ref('transcription_diarized_segment'),
            ref('transcription_error')]))


def source_sha(sdk):
    for name, expected in SDK_SOURCES.items():
        path = Path(sdk)/'node_modules/openai'/name
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('audio SDK source drift: '+name)
    return SDK_SOURCES[SDK_FILE]


def add_openapi(paths, response, error):
    from tools.serve_protocol.event_schema import event_stream
    external = lambda name: {'$ref':'serve-application-v1.schema.json#/$defs/'+name}
    success = response(dict(anyOf=[external('transcription_response'),
                                   external('transcription_verbose_response'),
                                   external('transcription_diarized_response')]))
    success['content'].update({
        'text/plain':dict(schema=dict(type='string')),
        'text/vtt':dict(schema=dict(type='string')),
        'application/x-subrip':dict(schema=dict(type='string')),
        'text/event-stream':event_stream(external('transcription_event'), **{
            'x-q4t-stream-contract':'transcript.text.delta, diarized segment events, then transcript.text.done; '
                'actual token usage, no Chat DONE sentinel; typed error terminates failure'})})
    paths['/v1/audio/transcriptions'] = dict(post=dict(
        operationId='v1_audio_transcriptions',
        requestBody=dict(required=True, content={'multipart/form-data':dict(
            schema=external('transcription_create'))}),
        responses={'200':success, 'default':response(error, 'Finite transcription error')},
        **{'x-q4t':dict(profile='q4t-native-asr-v1', model='qwen3-asr-0.6b',
            configured_by='asr-model-dir', binary_file_bytes=25*1024**2,
            multipart_bytes=32*1024**2, core_readiness_dependency=False,
            recovery='request-driven asset retry; no automatic primary model restart',
            implemented_formats=['json', 'text', 'verbose_json', 'srt', 'vtt', 'diarized_json'],
            required_pending_executors=['overlap_diarization'],
            diarization=dict(configured_by='speaker-model-dir',
                requires=['asr-model-dir','aligner-model-dir','vad-model-dir'],
                backend='native ECAPA FP32, request-local cosine average linkage',
                references='up to 4 inline audio data URLs, 2 to 10 seconds each',
                timing='actual aligner words assigned by acoustic interval overlap',
                streaming='completed segment_id delta and segment, then one complete done',
                overlap=dict(configured_by='separator-model-dir',
                    requires=['speaker-model-dir'], model='speechbrain-sepformer-wsj02mix',
                    backend='native SepFormer FP32', simultaneous_sources=2,
                    sample_rate=8000, output_sample_rate=16000,
                    timing='original time, crossing segments retained',
                    identity='cross-track actual ECAPA embeddings',
                    quotas='one original RPM/output/VAD/window budget for both tracks',
                    acceptance='actual mixed-voice HTTP and recovery remain pending')),
            alignment=dict(configured_by='aligner-model-dir',
                timestamps='actual fixed-model word edges; repaired ordering is explicit',
                segments='independently decoded clip with actual token/temperature/probability/compression'),
            chunking=dict(configured_by='vad-model-dir', backend='native CPU Silero 16k',
                strategies=['auto', 'server_vad'], max_chunks='explicit vad-max-chunks, 1..256',
                budget='one request RPM, cumulative input/output and one output ceiling',
                failure='no partial success or dropped tail',
                offsets='real original sample origin; seek in 10ms feature frames'),
            multipart_arrays='repeated bracket names; nested chunking_strategy bracket fields or JSON object',
            zero_temperature='native confidence policy; not a claim about private provider thresholds')}))

    translated = response(dict(anyOf=[external('translation_response'),
                                      external('translation_verbose_response')]))
    for media in ('text/plain', 'text/vtt', 'application/x-subrip'):
        translated['content'][media] = dict(schema=dict(type='string'))
    paths['/v1/audio/translations'] = dict(post=dict(
        operationId='createTranslation',
        requestBody=dict(required=True, content={'multipart/form-data':dict(
            schema=external('translation_create'))}),
        responses={'200':translated,'default':response(error,'Audio translation error')},
        **{'x-q4t':dict(backend='qwen3-asr-0.6b followed by the resident native Chat model',
            accepted_model_ids=['whisper-1','qwen3-asr-0.6b'],
            output_language='english', required_for_subtitles='aligner-model-dir',
            quota='one request and one cumulative source and translation budget')}))
