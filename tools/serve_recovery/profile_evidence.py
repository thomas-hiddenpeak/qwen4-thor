"""Verify retained profile observations and actual business result contracts."""
import hashlib
import json
import math

from tools.serve_recovery import ledger, profile_contract

FORMAT = 'q4t-profile-business-v1'


def message(response, finish):
    ledger.require(type(response) is dict and type(response.get('choices')) is list and
        len(response['choices']) == 1 and response['choices'][0].get('finish_reason') == finish,
        'profile business completion missing')
    value = response['choices'][0]['message']
    ledger.require(type(value) is dict and value.get('role') == 'assistant',
                   'profile business assistant missing')
    return value


def business(value, identity):
    ledger.keys(value, ('schema', 'tool_call', 'tool_result', 'tool_reply',
                       'tool_source', 'embedding'))
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
    ledger.require(value['format'] == FORMAT and value['profile'] == name and
        value['identity'] == registry['identity'] and
        value['capacity'] == registry['profiles'][name], 'profile evidence identity differs')
    actual = profile_contract.qualification(name, value['capacity'], value['identity'],
        value['capabilities'], value['configuration'], value['startup_memory'], digest)
    ledger.require(actual == registry['qualifications'][name],
                   'profile receipt does not match retained observations')
    business(value['business'], value['identity'])
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
