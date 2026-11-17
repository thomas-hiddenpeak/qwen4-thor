"""Native ASR multipart, finite output and typed streaming declarations."""
import hashlib
from pathlib import Path

SDK_FILE = 'src/resources/audio/transcriptions.ts'
SDK_SOURCES = {SDK_FILE:
    '8a81d4cb4c975cf027f78b8d86feb2fb39e7860b265433a91ebf13bef9f61d80'}


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
                 'properties':{'response_format':{'const':'verbose_json'}}}}]
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
                     logprobs=probabilities), ['type', 'delta'])
    done = obj(dict(response_fields, type=dict(const='transcript.text.done')),
               ['type', 'text', 'usage'])
    error = obj(dict(type=dict(const='error'), code=text, message=text,
                     param=nullable(text)), ['type', 'code', 'message', 'param'])
    return dict(transcription_create=request, transcription_response=response,
        transcription_logprob=probability, transcription_delta=delta,
        transcription_done=done, transcription_error=error,
        transcription_event=dict(anyOf=[ref('transcription_delta'),
            ref('transcription_done'), ref('transcription_error')]))


def source_sha(sdk):
    path = Path(sdk)/'node_modules/openai'/SDK_FILE
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != SDK_SOURCES[SDK_FILE]:
        raise ValueError('transcriptions SDK source drift')
    return digest


def add_openapi(paths, response, error):
    external = lambda name: {'$ref':'serve-application-v1.schema.json#/$defs/'+name}
    success = response(external('transcription_response'))
    success['content'].update({
        'text/plain':dict(schema=dict(type='string')),
        'text/event-stream':dict(schema=dict(type='string'), **{
            'x-q4t-event-schema':external('transcription_event'),
            'x-q4t-stream-contract':'transcript.text.delta then transcript.text.done; '
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
            implemented_formats=['json', 'text'],
            required_pending_executors=['actual_alignment', 'diarization', 'vad_chunking'],
            multipart_arrays='repeated bracket names; nested chunking_strategy bracket fields or JSON object',
            zero_temperature='native confidence policy; not a claim about private provider thresholds')}))
