"""Verify retained profile observations and actual business result contracts."""
import base64
import hashlib
import json
import math

from tools.serve_recovery import ledger, profile_contract

FORMAT = 'q4t-profile-business-v1'
MEDIA_FORMAT = 'q4t-profile-business-v2'
ASR_FORMAT = 'q4t-profile-business-v3'
ASR_FILE_SHA = '46dbc998c9d1d48111267c40741dd3200f2e5bcf4075f8c4c97f4451160dce50'
ASR_TEXT = '甚至出现交易几乎停滞的情况。'
MEDIA_FILE = 'tests/fixtures/file_video/transition-vp8.webm'
MEDIA_SHA = 'c1fddb4c848b966195302d193392a22856710fc6eb50640d891673f26de4b408'


def format_for(profile, identity=None):
    if (identity or {}).get('native_asr') is not None:
        return ASR_FORMAT
    return MEDIA_FORMAT if profile.get('allow_media', False) else FORMAT


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
                       (('asr',) if identity.get('native_asr') is not None else ()))
    if media:
        media_business(value['media'])
    if identity.get('native_asr') is not None:
        audio_business(value['asr'], identity)
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
