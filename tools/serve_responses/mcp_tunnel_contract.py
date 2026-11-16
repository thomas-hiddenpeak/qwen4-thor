"""Native reverse MCP tunnel wire, pinned to the reviewed 2026-08-25 protocol."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = 'serve-mcp-tunnel-v1.schema.json'


def obj(properties, required=(), **extra):
    return dict(type='object', properties=properties, required=list(required),
                **extra)


def ref(name):
    return {'$ref': '#/$defs/'+name}


def schema():
    token = dict(type='string', minLength=1, maxLength=128,
                 pattern=r'^[\x21-\x7e]+(?![\s\S])')
    route = dict(type='string', minLength=1, maxLength=128,
                 pattern=r'^[A-Za-z0-9_-]+(?![\s\S])')
    channel = dict(type='string', minLength=1, maxLength=512,
                   pattern=r'^[\x21-\x7e]+(?![\s\S])', **{'not': dict(const='*')})
    headers = dict(type='object', maxProperties=64,
        propertyNames=dict(type='string', minLength=1, maxLength=128,
            pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+(?![\s\S])"),
        additionalProperties=dict(type='array', maxItems=16,
            items=dict(type='string', pattern=r'^[\t\x20-\x7e\x80-\xff]*(?![\s\S])')),
        **{'x-q4t-total-bytes': 16384,
           'description': 'HTTP Latin-1 header arrays. Hop-by-hop and Connection-nominated '
                          'headers are removed; response headers use a fixed MCP allowlist.'})
    rpc_id = dict(anyOf=[dict(type='string'), dict(type='integer')])
    rpc_request = obj(dict(jsonrpc=dict(const='2.0'), id=rpc_id,
        method=dict(type='string', minLength=1, maxLength=256,
                    pattern=r'^[\x21-\x7e]+(?![\s\S])'), params={}), ['jsonrpc', 'method'])
    rpc_error = obj(dict(code=dict(type='integer'), message=dict(type='string'),
                         data={}), ['code', 'message'])
    rpc_response = obj(dict(jsonrpc=dict(const='2.0'), id=rpc_id,
        result={}, error=rpc_error, method=False), ['jsonrpc', 'id'],
        oneOf=[dict(required=['result'], **{'not': dict(required=['error'])}),
               dict(required=['error'], **{'not': dict(required=['result'])})])
    rpc_notify = dict(allOf=[rpc_request,
        dict(properties=dict(id=False, result=False, error=False))])
    command = obj(dict(request_id=token, shard_token=token,
        command_type=dict(enum=['jsonrpc', 'session_termination']),
        channel=channel, headers=headers, created_at=dict(type='string', format='date-time'),
        response_timeout=dict(type='string', pattern=r'^[0-9]+ms(?![\s\S])'),
        jsonrpc=rpc_request), ['request_id', 'shard_token', 'command_type',
                               'channel', 'headers', 'created_at', 'response_timeout'],
        allOf=[{'if': dict(properties=dict(command_type=dict(const='jsonrpc'))),
                'then': dict(required=['jsonrpc']),
                'else': dict(properties=dict(jsonrpc=False))}])
    upload = obj(dict(request_id=token, resp_code=dict(type='integer', minimum=100, maximum=599),
        resp_type=dict(enum=['jsonrpc_response', 'jsonrpc_notify', 'notify_ack',
                             'session_termination_response'], default='jsonrpc_response'),
        channel=dict(channel, default='main'), resp_headers=headers,
        resp_json={}, rpc_resp={}, shard_token=False,
        q4t_outcome_unknown=dict(type='boolean',
            description='Q4T extension: transport failed after possible dispatch; never replay.')),
        ['request_id', 'resp_code'],
        **{'not': dict(required=['resp_json', 'rpc_resp'])})
    upload['allOf'] = []
    for name, rpc in [('jsonrpc_response', rpc_response), ('jsonrpc_notify', rpc_notify)]:
        condition = dict(properties=dict(resp_type=dict(const=name)))
        if name != 'jsonrpc_response':
            condition['required'] = ['resp_type']
        upload['allOf'].append({'if': condition, 'then': dict(
            anyOf=[dict(required=['resp_json']), dict(required=['rpc_resp'])],
            properties=dict(resp_json=rpc, rpc_resp=rpc))})
    upload['allOf'].extend([
        {'if': dict(required=['resp_type'], properties=dict(resp_type=dict(
            enum=['notify_ack', 'session_termination_response']))),
         'then': dict(properties=dict(resp_json=False, rpc_resp=False))},
        {'if': dict(required=['q4t_outcome_unknown'],
                    properties=dict(q4t_outcome_unknown=dict(const=True))),
         'then': dict(properties=dict(resp_code=dict(minimum=400),
             resp_type=dict(const='jsonrpc_response'),
             resp_json=dict(required=['error']), rpc_resp=dict(required=['error'])))}])
    definitions = dict(route_id=route, header_token=token, channel=channel,
        metadata=obj(dict(id=route, object=dict(const='tunnel'), name=route,
            status=dict(enum=['active', 'stopped']), proc_affinity=dict(const=True)),
            ['id', 'object', 'name', 'status', 'proc_affinity']),
        command=command, poll=obj(dict(commands=dict(type='array', minItems=1,
            maxItems=25, items=ref('command'))), ['commands']),
        upload=upload, ack=obj(dict(status=dict(const='ok')), ['status']))
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$id': DOCUMENT, 'title': 'Q4T native MCP tunnel transport', '$defs': definitions,
        'x-q4t': dict(protocol_date='2026-08-25', source_commit=
            'fb6c47fbdf156451137e2c00a55d485be42f03a3',
            source='https://github.com/openai/tunnel-client/blob/'
                'fb6c47fbdf156451137e2c00a55d485be42f03a3/docs/protocol.md',
            max_upload_bytes=4194304, max_poll_bytes=8388608,
            stateful_checks='Exact agent/caller/generation/instance/channel/token/RPC ID; '
                'one-way handoff; duplicate final only acknowledges identical terminal.',
            unknown_metadata='Ignored within byte/depth/value limits; never establishes authority.',
            invalid_authenticated_terminal='400 plus fixed 502 result for caller, with '
                'q4t_outcome_unknown on RPC requests; invalid progress cannot finish a request.')}


def add_openapi(paths, response, error):
    external = lambda name: {'$ref': DOCUMENT+'#/$defs/'+name}
    path = dict(name='tunnel_id', **{'in': 'path'}, required=True,
                schema=external('route_id'))
    instance = dict(name='X-Tunnel-Client-Instance-Id', **{'in': 'header'},
        required=True, schema=external('header_token'),
        description='Single field, exact instance; duplicate or Connection-nominated fields rejected.')
    root = '/v1/tunnels/{tunnel_id}'
    for suffix, method, result, lane in [('', 'get', 'metadata', 'control'),
            ('/poll', 'get', 'poll', 'tunnel_poll'),
            ('/response', 'post', 'ack', 'tunnel_response')]:
        operation = dict(operationId='v1_tunnels_'+(suffix[1:] or 'retrieve'),
            parameters=[path], responses={'200': response(external(result)),
                'default': response(error, 'Authenticated, bounded tunnel error')},
            **{'x-q4t': dict(lane=lane, auth='Configured independent API Key; backend '
                'also checks exact tunnel agent/caller scope. No client identity from request JSON.',
                no_automatic_tool_replay=True)})
        if suffix == '/poll':
            operation['parameters'] += [instance,
                dict(name='limit', **{'in': 'query'}, schema=dict(type='integer',
                    minimum=1, maximum=25, default=25)),
                dict(name='timeout_ms', **{'in': 'query'}, schema=dict(type='integer',
                    minimum=0, maximum=15000, default=15000)),
                dict(name='channel', **{'in': 'query'}, style='form', explode=True,
                    schema=dict(type='array', minItems=1, maxItems=16,
                        uniqueItems=True, items=external('channel')))]
            operation['responses']['204'] = dict(description='No commands before poll deadline; no body.')
            operation['x-q4t-query-contract'] = ('Only declared fields. channel repeats; '
                'limit/timeout_ms once; max 8192 bytes; no body or shard token.')
        elif suffix == '/response':
            operation['parameters'] += [instance, dict(name='X-Tunnel-Shard-Token',
                **{'in': 'header'}, required=True, schema=external('header_token'))]
            operation['requestBody'] = dict(required=True, content={'application/json': dict(
                schema=external('upload'))})
            operation['x-q4t-query-contract'] = 'No query fields.'
            operation['x-q4t-body-contract'] = 'Max 4 MiB; no duplicate JSON keys; depth 96, 65536 values.'
        else:
            operation['x-q4t-query-contract'] = 'No query fields, body or shard token.'
        paths[root+suffix] = {method: operation}


if __name__ == '__main__':
    (ROOT/'docs'/DOCUMENT).write_text(json.dumps(schema(), ensure_ascii=False, indent=2)+'\n')
