"""Publish native application wire shapes from the pinned SDK type graph."""
import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DRAFT = 'https://json-schema.org/draft/2020-12/schema'


def ref(name):
    return {'$ref': '#/$defs/'+str(name)}


def obj(properties, required=()):
    return dict(type='object', properties=properties, required=list(required),
                additionalProperties=False)


def nullable(value):
    return dict(anyOf=[value, dict(type='null')])


def schema(graph):
    assert graph['profile'] == 'q4t-openai-application-types-v1'
    definitions = {}
    for index, node in enumerate(graph['nodes']):
        kind = node['kind']
        if kind in ('string', 'number', 'boolean', 'null'):
            value = dict(type=kind)
        elif kind == 'any':
            value = {}
        elif kind == 'never':
            value = False
        elif kind == 'literal':
            value = dict(const=node['value'])
        elif kind == 'union':
            value = dict(anyOf=[ref(i) for i in node['variants']])
        elif kind == 'array':
            value = dict(type='array', items=ref(node['element']))
        elif kind == 'tuple':
            value = dict(type='array', prefixItems=[ref(i) for i in node['elements']],
                         minItems=node['required'], maxItems=len(node['elements']))
        elif kind == 'object':
            value = obj({f['name']: ref(f['type']) for f in node['fields']},
                        [f['name'] for f in node['fields'] if not f['optional']])
            if node['additional'] is not None:
                value['additionalProperties'] = ref(node['additional'])
        else:
            raise ValueError('unhandled SDK type: '+kind)
        definitions[str(index)] = value
    for name, index in graph['roots'].items():
        definitions[name] = ref(index)
    from tools.serve_responses.files_contract import definitions as file_definitions
    definitions.update(file_definitions())

    # The current official cache guide adds a 12h request TTL; the pinned SDK
    # already permits 12h on individual blocks but has a stale request/output
    # TTL literal. Adapt only the cache-options objects, preserving the graph.
    for index, node in enumerate(graph['nodes']):
        fields = {f['name'] for f in node.get('fields', [])}
        if ({'mode', 'ttl'} <= fields and 'comparison_response_id' in fields
                and fields <= {'mode', 'ttl', 'prewarm', 'comparison_response_id'}):
            definitions[str(index)]['properties']['ttl'] = dict(enum=['30m', '12h'])

    def root(name):
        return definitions[str(graph['roots'][name])]

    # The SDK declares OutputItem[] here but its own comment and the official
    # compact guide require preserved user messages and a replayable input
    # window. Adapt only the compact result, never ordinary response outputs.
    root('compacted_response')['properties']['output'] = dict(
        type='array', maxItems=4096, items=ref('input_item'))

    # The pinned SDK describes include_obfuscation on streaming deltas but
    # omits that optional property from most delta interfaces. Preserve the
    # documented wire field only for delta events, with strict other events.
    for index in graph['nodes'][graph['roots']['response_event']]['variants']:
        node = graph['nodes'][index]
        tag = next((graph['nodes'][field['type']].get('value')
                    for field in node.get('fields', []) if field['name'] == 'type'), None)
        if isinstance(tag, str) and tag.endswith('.delta'):
            definitions[str(index)]['properties']['obfuscation'] = dict(type='string')

    # output_text is added by the SDK after parsing; it is not a wire field.
    response = root('response')
    response['required'].remove('output_text')
    response['properties']['store'] = dict(type='boolean')
    # Native auxiliary classification is separately disclosed and charged to
    # the same client TPM lease; ordinary usage remains business generation.
    response['properties']['q4t_moderation_usage'] = obj(dict(
        input_tokens=dict(type='integer', minimum=0),
        output_tokens=dict(type='integer', minimum=0, maximum=1024),
        total_tokens=dict(type='integer', minimum=0),
        model_steps=dict(type='integer', minimum=0, maximum=2)),
        ['input_tokens', 'output_tokens', 'total_tokens', 'model_steps'])
    response['properties']['q4t_access_program'] = obj(dict(
        authority=dict(const='q4t-local'),
        policy_sha256=dict(type='string', pattern='^[0-9a-f]{64}$'),
        backend_model=dict(type='string', minLength=1),
        treatment=dict(enum=['standard', 'operator_system_instructions'])),
        ['authority', 'policy_sha256', 'backend_model', 'treatment'])
    # The pinned TypeScript interface omits null, but official HTTP examples
    # explicitly return usage:null before completion and after cancellation.
    response['properties']['usage'] = nullable(response['properties']['usage'])
    response['allOf'] = [{'if': dict(required=['status'], properties=dict(
        status=dict(enum=['completed', 'incomplete']))), 'then': dict(
        required=['usage'], properties=dict(usage=dict(type='object')))}]
    # Q4T codes identify the actual native failure instead of replacing it
    # with an unrelated hosted-provider error. Keep the remaining shape.
    error_union = graph['nodes'][next(f['type'] for f in
        graph['nodes'][graph['roots']['response']]['fields'] if f['name'] == 'error')]
    for index in error_union['variants']:
        node = definitions[str(index)]
        if isinstance(node, dict) and node.get('type') == 'object':
            node['properties']['code'] = dict(type='string', minLength=1)

    request = root('response_create')
    # The SDK exposes this model style option only on count. Native create
    # accepts the same real preset so counted and generated prompts agree.
    personality = dict(type='string', enum=['friendly', 'pragmatic'], maxLength=64)
    request['properties']['personality'] = personality
    response['properties']['personality'] = personality
    root('input_tokens_request')['properties']['personality'] = personality
    root('input_tokens_response')['properties']['input_tokens'] = dict(
        type='integer', minimum=0)
    request['properties'].update(
        seed=nullable(dict(type='integer', minimum=-(2**63), maximum=2**63-1)),
        request_id=dict(type='string', minLength=1, maxLength=128,
                        pattern='^[A-Za-z0-9_-]+$'),
        cancel_token=dict(type='string', pattern='^[0-9a-f]{64}$'),
        request_timeout_ms=nullable(dict(type='integer', minimum=1, maximum=10800000)))
    request['properties']['prompt_cache_key'] = nullable(dict(type='string', maxLength=512))
    request['dependentRequired'] = dict(request_id=['cancel_token'],
                                        cancel_token=['request_id'])
    for name, low, high in [('max_output_tokens', 1, 2147483647),
                          ('max_tool_calls', 1, 2147483647), ('top_logprobs', 0, 20)]:
        request['properties'][name] = nullable(dict(type='integer', minimum=low,
                                                   maximum=high))
    for name, high in [('temperature', 2), ('top_p', 1)]:
        request['properties'][name] = nullable(dict(type='number', minimum=0,
                                                   maximum=high))
    request['properties']['metadata'] = nullable(dict(type='object', maxProperties=16,
        propertyNames=dict(maxLength=64), additionalProperties=dict(type='string', maxLength=512)))
    request['allOf'] = [
        {'not': dict(required=['previous_response_id', 'conversation'],
            properties={name: {'not': dict(type='null')}
                        for name in ('previous_response_id', 'conversation')})},
        {'if': dict(required=['stream_options'],
            properties=dict(stream_options={'not': dict(type='null')})),
         'then': dict(required=['stream'], properties=dict(stream=dict(const=True)))}]
    root('input_tokens_request')['allOf'] = [copy.deepcopy(request['allOf'][0])]
    for name in ('response_retrieve', 'input_items_query', 'conversation_items_query'):
        value = root(name)
        if 'limit' in value['properties']:
            value['properties']['limit'] = dict(type='integer', minimum=1, maximum=100)
        if 'starting_after' in value['properties']:
            value['properties']['starting_after'] = dict(type='integer', minimum=0)
            value['allOf'] = [{'if': dict(required=['starting_after']),
                'then': dict(required=['stream'], properties=dict(stream=dict(const=True)))}]
    # The SDK combines body and query in this parameter interface. The wire
    # body is closed; include belongs exclusively to the URL query.
    create = root('conversation_items_create')
    include = create['properties'].pop('include')
    definitions['item_include_query'] = obj(dict(include=include))
    create['properties']['items'] = dict(type='array', maxItems=20, items=ref('input_item'))
    root('conversation_create')['properties']['items'] = nullable(
        dict(type='array', maxItems=20, items=ref('input_item')))
    for name in ('conversation_create', 'conversation_update'):
        root(name)['properties']['metadata'] = copy.deepcopy(request['properties']['metadata'])
    definitions['response_deleted'] = obj(dict(id=dict(type='string'),
        object=dict(const='response.deleted'), deleted=dict(const=True)),
        ('id', 'object', 'deleted'))
    from tools.serve_chat_storage.build_contract import definitions as chat_definitions
    definitions.update(chat_definitions())
    from tools.serve_responses.prompt_contract import definitions as prompt_definitions
    definitions.update(prompt_definitions(request['properties']))
    from tools.serve_responses.files_contract import SDK_FILES_SHA256
    return {'$schema': DRAFT, 'title': 'Q4T Chat, Responses, Conversations and Files wire contract',
        '$defs': definitions, 'x-q4t': dict(profile='q4t-application-wire-v1',
            sdk_version=graph['sdk_version'], compiler_version=graph['compiler_version'],
            sources_sha256={**graph['sources_sha256'],
                            'src/resources/files.ts': SDK_FILES_SHA256},
            license_sha256=graph['license_sha256'],
            compiler_sha256=graph['compiler_sha256'],
            semantics='Structural shapes include required planned capabilities. Shape validity does not promise that a model, tool or state executor is available. Check native planning and runtime capabilities; pending capabilities remain required implementation tasks.',
            adaptations=['SDK-added output_text is optional on wire',
                'FileObject.purpose includes evals already accepted by the SDK FilePurpose input; preserve the actual purpose',
                'Files multipart uses binary file and bracket-encoded expires_after fields; explicit native capacity and storage quotas remain separate',
                'official HTTP examples use usage:null for active/cancelled responses; completed/incomplete require actual usage',
                'native store echo and native response error codes',
                'count endpoint uses actual model templates; native create also accepts the model-owned friendly/pragmatic personality presets from the count SDK contract',
                'optional q4t_moderation_usage discloses bounded auxiliary classifier tokens separately from business usage',
                'SDK-documented include_obfuscation adds optional string padding to delta events only',
                'official prompt caching guide permits 12h on request/applied options in addition to the pinned SDK 30m literal',
                'exact integer and bounded native options',
                'native cancellation credentials and timeout/seed extensions',
                'SDK combined body/query split on actual HTTP boundary',
                'compact output is a replayable input window including preserved user messages; only this SDK OutputItem[] inconsistency is adapted'],
            wire_references=[dict(date='2026-11-10',
                url='https://developers.openai.com/api/docs/guides/prompt-caching',
                contract='Request and breakpoint TTL 30m/12h; exact eligible boundaries; overlapping read/write token accounting'),
                dict(date='2026-11-10',
                url='https://developers.openai.com/api/docs/guides/prompt-caching/diagnostics',
                contract='Optional client-scoped short-lived hashes; unavailable never fails generation; terminal SSE carries comparison'),
                dict(date='2026-11-09',
                url='https://developers.openai.com/api/reference/resources/responses/methods/cancel',
                contract='Cancelled resource can have usage:null; no fabricated zero usage'),
                dict(date='2026-11-09',
                url='https://developers.openai.com/api/docs/guides/compaction',
                contract='Standalone compact preserves user messages and returns the canonical next input window with encrypted compaction state'),
                dict(date='2026-11-09',
                url='https://developers.openai.com/api/reference/resources/responses/methods/create',
                contract='response.created example has usage:null; successful final usage includes cache_write_tokens')],
            response_terminals=['completed', 'incomplete', 'failed', 'cancelled'],
            cancelled_stream='Resource is cancelled; original events end without fabricated response.cancelled/completed or Chat DONE.',
            query='Closed route-specific fields; repeated include[] is an array; exact decimal integers and true/false booleans.')}


def add_openapi(paths, response, error):
    """Add actual application routes, preserving each HTTP method/body/query."""
    from tools.serve_responses.files_contract import add_openapi as add_files
    add_files(paths, response, error)
    document = json.loads((ROOT/'docs/serve-application-v1.schema.json').read_text())
    external = lambda name: {'$ref': 'serve-application-v1.schema.json#/$defs/'+name}

    def shape(name):
        value = document['$defs'][name]
        while '$ref' in value:
            value = document['$defs'][value['$ref'].split('/')[-1]]
        return value

    def query_fields(name):
        def rewrite(value):
            if isinstance(value, dict):
                return {k: ('serve-application-v1.schema.json'+v if k == '$ref'
                            else rewrite(v)) for k, v in value.items()}
            if isinstance(value, list):
                return [rewrite(v) for v in value]
            return value
        result = []
        for key, value in shape(name)['properties'].items():
            result.append(dict(name='include[]' if key == 'include' else key,
                **{'in': 'query'}, required=False, schema=rewrite(value),
                style='deepObject' if key == 'metadata' else 'form', explode=True))
        return result

    endpoints = [
        ('/v1/prompts', 'post', 'prompt_create', 'prompt_resource', None),
        ('/v1/prompts', 'get', None, 'prompt_list', 'prompt_query'),
        ('/v1/prompts/{prompt_id}', 'get', None, 'prompt_resource', None),
        ('/v1/prompts/{prompt_id}', 'post', 'prompt_select', 'prompt_resource', None),
        ('/v1/prompts/{prompt_id}', 'delete', None, 'prompt_deleted', None),
        ('/v1/prompts/{prompt_id}/versions', 'post', 'prompt_append', 'prompt_resource', None),
        ('/v1/prompts/{prompt_id}/versions/{version}', 'get', None, 'prompt_version', None),
        ('/v1/responses', 'post', 'response_create', 'response', None),
        ('/v1/responses/compact', 'post', 'response_compact', 'compacted_response', None),
        ('/v1/responses/input_tokens', 'post', 'input_tokens_request', 'input_tokens_response', None),
        ('/v1/responses/{response_id}', 'get', None, 'response', 'response_retrieve'),
        ('/v1/responses/{response_id}', 'delete', None, 'response_deleted', None),
        ('/v1/responses/{response_id}/cancel', 'post', None, 'response', None),
        ('/v1/responses/{response_id}/input_items', 'get', None, 'input_items_list', 'input_items_query'),
        ('/v1/conversations', 'post', 'conversation_create', 'conversation', None),
        ('/v1/conversations/{conversation_id}', 'get', None, 'conversation', None),
        ('/v1/conversations/{conversation_id}', 'post', 'conversation_update', 'conversation', None),
        ('/v1/conversations/{conversation_id}', 'delete', None, 'conversation_deleted', None),
        ('/v1/conversations/{conversation_id}/items', 'post', 'conversation_items_create', 'conversation_items', 'item_include_query'),
        ('/v1/conversations/{conversation_id}/items', 'get', None, 'conversation_items', 'conversation_items_query'),
        ('/v1/conversations/{conversation_id}/items/{item_id}', 'get', None, 'conversation_item', 'item_include_query'),
        ('/v1/conversations/{conversation_id}/items/{item_id}', 'delete', None, 'conversation', None),
        ('/v1/chat/completions', 'get', None, 'stored_chat_list', 'stored_chat_query'),
        ('/v1/chat/completions/{completion_id}', 'get', None, 'stored_chat', None),
        ('/v1/chat/completions/{completion_id}', 'post', 'stored_chat_update', 'stored_chat', None),
        ('/v1/chat/completions/{completion_id}', 'delete', None, 'stored_chat_deleted', None),
        ('/v1/chat/completions/{completion_id}/messages', 'get', None, 'stored_chat_messages', 'stored_chat_messages_query')]
    for path, method, body, result, query in endpoints:
        parameters = [dict(name=part[1:-1], **{'in': 'path'}, required=True,
                           schema=dict(type='string', minLength=1))
                      for part in path.split('/') if part.startswith('{')]
        if query:
            parameters += query_fields(query)
        success = response(external(result))
        operation = dict(operationId=method+'_'+path.strip('/').replace('/', '_').replace('{', '').replace('}', ''),
            parameters=parameters, responses={'200': success,
                'default': response(error, 'Native error; unknown/other-client identities return 404')},
            **{'x-q4t-query-contract': query or 'no query',
               'x-q4t-body-contract': body or 'empty body only'})
        if body:
            optional = path in ('/v1/conversations', '/v1/responses/input_tokens')
            operation['requestBody'] = dict(required=not optional,
                content={'application/json': dict(schema=nullable(external(body))
                    if optional else external(body))})
        if result == 'response' and not path.endswith('/cancel'):
            success['content']['text/event-stream'] = dict(schema=dict(type='string'),
                **{'x-q4t-event-schema': external('response_event'),
                   'x-q4t-stream-contract': 'Native sequenced Responses events. No Chat DONE. Persisted original replay with starting_after; background requires original stream=true. Disconnect does not cancel a background job. Cancelled resource retains partial items and ends original events without a fabricated completed/cancelled event.'})
        if path.endswith('/cancel'):
            operation['description'] = 'Idempotent native background cancellation, empty body, 200 real resource. Independent bounded cancellation admission.'
        if path.startswith('/v1/prompts'):
            operation['description'] = 'q4t native Prompt registry, scoped to authenticated client. Immutable versions; revision-conditional append/default selection; durable effective request snapshots. Does not access hosted prompt accounts.'
        paths.setdefault(path, {})[method] = operation


def standalone_openapi(spec):
    """Make the shipped OpenAPI self-contained, without fetching schemas."""
    components = {}

    def rewrite(value, owner=None):
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if key in ('$schema', '$id') and isinstance(child, str):
                    continue
                if key == '$ref' and isinstance(child, str):
                    file, sep, fragment = child.partition('#')
                    if file:
                        if '/' in file or not file.endswith('.json'):
                            raise ValueError('unreviewed external schema '+file)
                        if file not in components:
                            components[file] = {}  # Reserve before recursive references.
                            components[file] = rewrite(json.loads((ROOT/'docs'/file).read_text()), file)
                        target = file
                    else:
                        target = owner
                    if not target or (sep and fragment and not fragment.startswith('/')):
                        raise ValueError('unresolved reference '+child)
                    result[key] = '#/components/schemas/'+target+fragment
                else:
                    result[key] = rewrite(child, owner)
            return result
        if isinstance(value, list):
            return [rewrite(child, owner) for child in value]
        return value

    bundled = rewrite(spec)
    bundled['components'] = dict(schemas=components)
    return bundled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--types', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    graph = json.loads(args.types.read_text())
    result = schema(graph)
    result['x-q4t']['type_graph_sha256'] = hashlib.sha256(args.types.read_bytes()).hexdigest()
    destination = ROOT/'docs/serve-application-v1.schema.json'
    data = json.dumps(result, ensure_ascii=False, indent=2)+'\n'
    if args.check:
        assert destination.read_text() == data, 'application schema drift'
    else:
        destination.write_text(data)
    print(json.dumps(dict(path=str(destination), definitions=len(result['$defs']),
                         bytes=len(data.encode()))))


if __name__ == '__main__':
    main()
