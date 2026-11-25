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


# The browser POST multiplexes these JSON replies and six SSE operations.
CHATKIT_BROWSER_JSON = {
    'attachments.create': 'chatkit_reserved_document',
    'attachments.delete': 'chatkit_empty_result',
    'input.transcribe': 'chatkit_transcription_result',
    'items.feedback': 'chatkit_empty_result',
    'items.list': 'chatkit_browser_output_items_page',
    'threads.delete': 'chatkit_empty_result',
    'threads.get_by_id': 'chatkit_browser_output_thread',
    'threads.list': 'chatkit_browser_output_threads_page',
    'threads.sync_custom_action': 'chatkit_browser_output_sync',
    'threads.update': 'chatkit_browser_output_thread',
}
CHATKIT_BROWSER_SSE = (
    'threads.create', 'threads.add_user_message', 'threads.add_client_tool_output',
    'threads.retry_after_item', 'threads.add_structured_input', 'threads.custom_action',
)


def ref(name):
    return {'$ref': '#/$defs/'+str(name)}


def obj(properties, required=()):
    return dict(type='object', properties=properties, required=list(required),
                additionalProperties=False)


def nullable(value):
    return dict(anyOf=[value, dict(type='null')])


def graph_definitions(graph, prefix='', roots=None):
    selected = graph['roots'] if roots is None else {name: graph['roots'][name] for name in roots}
    pending = list(reversed(range(len(graph['nodes'])))) if roots is None else list(selected.values())
    definitions = {}

    def node_ref(index):
        if roots is not None and prefix+str(index) not in definitions:
            pending.append(index)
        return ref(prefix+str(index))

    while pending:
        index = pending.pop()
        if prefix+str(index) in definitions:
            continue
        node = graph['nodes'][index]
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
            value = dict(anyOf=[node_ref(i) for i in node['variants']])
        elif kind == 'array':
            value = dict(type='array', items=node_ref(node['element']))
        elif kind == 'tuple':
            value = dict(type='array', prefixItems=[node_ref(i) for i in node['elements']],
                         minItems=node['required'], maxItems=len(node['elements']))
        elif kind == 'object':
            value = obj({f['name']: {**node_ref(f['type']), **f.get('bounds', {})} for f in node['fields']},
                        [f['name'] for f in node['fields'] if not f['optional']])
            if node['additional'] is not None:
                value['additionalProperties'] = node_ref(node['additional'])
        else:
            raise ValueError('unhandled SDK type: '+kind)
        definitions[prefix+str(index)] = value
    for name, index in selected.items():
        definitions[prefix+name] = ref(prefix+str(index))
    return definitions


def schema(graph):
    assert graph['profile'] == 'q4t-openai-application-types-v1'
    definitions = graph_definitions(graph)
    live = json.loads((ROOT/'src/server/contracts/openai_live_types_v1.json').read_text())
    assert live['profile'] == 'q4t-openai-live-types-v1'
    definitions.update(graph_definitions(live, 'live_', ('session_accept', 'session_refer', 'session_reject', 'session_fork', 'session_forked', 'media_create', 'media_created')))
    realtime = json.loads((ROOT/'src/server/contracts/openai_realtime_types_v1.json').read_text())
    assert realtime['profile'] == 'q4t-openai-realtime-types-v1'
    definitions.update(graph_definitions(realtime, 'realtime_', (
        'session', 'call_accept', 'call_refer', 'call_reject',
        'client_secret_create', 'client_secret_created', 'translation_secret_create',
        'translation_secret_created', 'translation_session', 'translation_session_create',
        'translation_session_update', 'translation_client_event', 'translation_server_event')))
    for request in ('realtime_client_secret_create', 'realtime_translation_secret_create'):
        definitions[request] = {'allOf':[definitions[request], {'properties':{
            'expires_after':{'properties':{'seconds':dict(type='integer', minimum=10,
                                                          maximum=7200, default=600)}}}}]}
    # The translation wire reference adds suppression strength to the fixed SDK.
    # Extend only the processor objects reachable from translation configurations.
    visited = set()
    pending = [realtime['roots'][name] for name in (
        'translation_session', 'translation_session_create', 'translation_session_update')]
    while pending:
        index = pending.pop()
        if index in visited:
            continue
        visited.add(index)
        node = realtime['nodes'][index]
        pending.extend(node.get('variants', []))
        for field in node.get('fields', []):
            pending.append(field['type'])
            if field['name'] == 'noise_reduction':
                for member in realtime['nodes'][field['type']]['variants']:
                    if realtime['nodes'][member]['kind'] == 'object':
                        definitions['realtime_'+str(member)]['properties']['strength'] = dict(
                            type='number', minimum=0, maximum=1, default=1)
    # Realtime wire settings allow explicitly clearing these processors.
    for index, node in enumerate(realtime['nodes']):
        if node.get('kind') != 'object':
            continue
        fields = definitions.get('realtime_'+str(index), {}).get('properties', {})
        if {'format','turn_detection','noise_reduction','transcription'} <= fields.keys():
            for key in ('transcription','noise_reduction'):
                fields[key] = nullable(fields[key])
    status_code = dict(type='integer', minimum=300, maximum=699)
    target_uri = {'maxLength':8192,
                  'pattern':r"^[A-Za-z][A-Za-z0-9+.\-]*:(?:[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=\-]|%[0-9A-Fa-f]{2})+(?![\s\S])"}
    for family in ('live_session_', 'realtime_call_'):
        status = dict(status_code, default=603) if family == 'realtime_call_' else status_code
        for operation, fields in [('refer', {'target_uri':target_uri}), ('reject', {'status_code':status})]:
            name = family + operation
            definitions[name] = {'allOf':[definitions[name], {'properties':fields}]}
    definitions['realtime_call_reject'] = nullable(definitions['realtime_call_reject'])
    agents = json.loads((ROOT/'src/server/contracts/openai_agent_registration_types_v1.json').read_text())
    assert agents['profile'] == 'q4t-openai-agent-registration-types-v1'
    definitions.update(graph_definitions(agents, 'agents_'))
    definitions['agents_list'] = obj(dict(object=dict(const='list'),
        data=dict(type='array', items=ref('agents_resource')), has_more=dict(type='boolean'),
        first_id=nullable(dict(type='string')), last_id=nullable(dict(type='string'))),
        ('object', 'data', 'has_more', 'first_id', 'last_id'))
    page_limit = dict(type='integer', minimum=1, maximum=100, default=20)
    descending_order = dict(enum=['asc', 'desc'], default='desc')
    definitions['agents_query'] = obj(dict(limit=page_limit,
        after=dict(type='string'), order=descending_order))
    evals = json.loads((ROOT/'src/server/contracts/openai_eval_types_v1.json').read_text())
    if evals['profile'] != 'q4t-openai-eval-types-v1':
        raise ValueError('Unexpected Evals type graph')
    definitions.update(graph_definitions(evals, 'eval_', tuple(evals['roots'])))
    # TypeScript's number includes fractions. The HTTP query parser accepts
    # exact integers and all three Eval listings allow an empty page.
    for name in ('query', 'run_query', 'output_query'):
        query = definitions['eval_' + str(evals['roots'][name])]
        fields = query['properties']
        fields['limit'] = dict(type='integer', minimum=0, default=20)
        fields['order']['default'] = 'asc'
        if name == 'query':
            fields['order_by']['default'] = 'created_at'
    for name, item in (('eval_list', 'eval_eval'), ('eval_run_list', 'eval_run'),
                       ('eval_output_list', 'eval_output_item')):
        definitions[name] = obj(dict(object=dict(const='list'),
            data=dict(type='array', items=ref(item)), first_id=dict(type='string'),
            last_id=dict(type='string'), has_more=dict(type='boolean')),
            ['object', 'data', 'first_id', 'last_id', 'has_more'])
    chatkit = json.loads((ROOT/'src/server/contracts/openai_chatkit_types_v1.json').read_text())
    if chatkit['profile'] != 'q4t-openai-chatkit-types-v1':
        raise ValueError('Unexpected ChatKit type graph')
    definitions.update(graph_definitions(chatkit, 'chatkit_'))
    for name in ('thread_query', 'item_query'):
        query = definitions['chatkit_' + str(chatkit['roots'][name])]
        query['properties']['limit']['default'] = 20
    definitions['chatkit_client_request'] = obj(dict(type=dict(type='string', enum=[
        'threads.create', 'threads.add_user_message', 'threads.add_client_tool_output',
        'threads.custom_action', 'threads.sync_custom_action',
        'threads.retry_after_item', 'threads.add_structured_input', 'threads.get_by_id',
        'threads.list', 'threads.delete', 'threads.update', 'items.list', 'items.feedback', 'attachments.create', 'attachments.delete', 'input.transcribe']),
        params=dict(type='object'), metadata=dict(type='object')), ['type', 'params'])
    string = dict(type='string')
    def browser_fields(properties, required):
        return dict(type='object', properties=properties, required=required)

    definitions['chatkit_browser_content'] = dict(oneOf=[
        browser_fields(dict(type=dict(const='input_text'), text=string), ['type', 'text']),
        browser_fields(dict(type=dict(const='input_tag'), id=string, text=string,
                            data=dict(type='object'), group=nullable(string),
                            interactive=dict(type='boolean')), ['type', 'id', 'text', 'data'])])
    definitions['chatkit_browser_message'] = browser_fields(dict(
        content=dict(type='array', items=ref('chatkit_browser_content')),
        attachments=dict(type='array', items=string), quoted_text=nullable(string),
        inference_options=dict(type='object')), ['content', 'attachments', 'inference_options'])
    message = ref('chatkit_browser_message')
    action_params = browser_fields(dict(thread_id=string, item_id=nullable(string),
        action=browser_fields(dict(type=dict(type='string', minLength=1), payload={}), ['type'])),
        ['thread_id', 'action'])
    form_answer = browser_fields(dict(values=dict(type='array', items=string),
                                      skipped=dict(type='boolean')), [])
    form_input = browser_fields(dict(status=dict(enum=['answered', 'skipped']),
        answers=dict(type='object', additionalProperties=form_answer)), [])
    feedback = browser_fields(dict(thread_id=string, item_ids=dict(type='array', items=string),
                                   kind=dict(enum=['positive', 'negative'])), ['thread_id', 'item_ids', 'kind'])
    browser_page = dict(
        limit=dict(anyOf=[dict(type='integer', minimum=0, maximum=100), dict(type='null')],
                   default=20, description='Zero or null selects the default of 20.'),
        order=dict(enum=['asc', 'desc'], default='desc'), after=nullable(string))
    definitions['chatkit_client_request']['allOf'] = [
        {'if': {'properties': {'type': {'const': name}}},
         'then': {'properties': {'params': params}}}
        for name, params in [
            ('attachments.create', browser_fields(dict(name=dict(type='string', minLength=1),
                size=dict(type='integer', minimum=0), mime_type=string), ['name', 'size', 'mime_type'])),
            ('input.transcribe', obj(dict(
                audio_base64=dict(type='string', minLength=4, maxLength=34952536,
                                  contentEncoding='base64'),
                mime_type=dict(type='string', minLength=1, maxLength=255)),
                ['audio_base64', 'mime_type'])),
            ('threads.list', browser_fields(browser_page, [])),
            ('items.list', browser_fields(dict(browser_page, thread_id=string), ['thread_id'])),
            ('threads.custom_action', action_params),
            ('threads.sync_custom_action', action_params),
            ('threads.add_structured_input', browser_fields(
                dict(thread_id=string, item_id=string, input=form_input),
                ['thread_id', 'item_id', 'input'])),
            ('threads.retry_after_item', browser_fields(
                dict(thread_id=string, item_id=string), ['thread_id', 'item_id'])),
            ('items.feedback', feedback), ('threads.create', browser_fields({'input': message}, ['input'])),
            ('threads.add_user_message', browser_fields(dict(input=message, thread_id=string), ['input', 'thread_id']))]]
    browser = json.loads((ROOT/'src/server/contracts/openai_chatkit_browser_output_v1.json').read_bytes())
    definitions.update(browser['definitions'])
    definitions['chatkit_empty_result'] = obj({})
    definitions['chatkit_client_result'] = dict(anyOf=[
        ref(name) for name in sorted(set(CHATKIT_BROWSER_JSON.values()))])
    definitions['chatkit_transcription_result'] = obj(dict(text=string), ['text'])
    definitions['chatkit_document_attachment'] = obj(dict(id=dict(type='string'),name=dict(type='string'),
        type=dict(const='file'),mime_type=dict(type='string')), ['id','name','type','mime_type'])
    definitions['chatkit_reserved_document'] = obj(dict(
        id=string, name=string, type=dict(const='file'), mime_type=string,
        upload_descriptor=obj(dict(url=dict(type='string',format='uri'), method=dict(const='PUT'),
            headers=obj({'Authorization': string, 'Content-Type': string}, ['Authorization','Content-Type'])),
            ['url','method','headers'])), ['id','name','type','mime_type','upload_descriptor'])
    definitions['chatkit_threads'] = obj(dict(object=dict(const='list'),
        data=dict(type='array', items=ref('chatkit_thread')),
        first_id=nullable(dict(type='string')), last_id=nullable(dict(type='string')),
        has_more=dict(type='boolean')), ['object','data','first_id','last_id','has_more'])
    safety_text = dict(type='string')
    safety_base = dict(id=safety_text, created_at=dict(type='integer', format='unixtime'),
                       reason=nullable(safety_text))
    definitions['video_character'] = obj(dict(id=nullable(dict(type='string')),
        name=nullable(dict(type='string')), created_at=dict(type='integer', format='unixtime')),
        ['id', 'name', 'created_at'])
    definitions['video_character_create'] = obj(dict(video=dict(type='string', format='binary'),
        name=dict(type='string', minLength=1, maxLength=80)), ['video','name'])
    safety_case = dict(safety_base, object=dict(const='safety.case'),
        entity_identifier=safety_text,
        notice=obj(dict(type=dict(enum=['warning', 'deactivation'])), ['type']))
    definitions['safety_case'] = obj(safety_case, safety_case)
    safety_alert = dict(safety_base, object=dict(const='safety.alert'), model=safety_text,
        request_id=safety_text, response_id=safety_text, request_paused=dict(type='boolean'),
        error_type=dict(enum=['potentially_unintended_data_access', 'potentially_unintended_data_transfer',
                              'potentially_unintended_destructive_activity', 'other']))
    definitions['safety_alert'] = obj(dict(safety_alert, detailed_explanation=nullable(safety_text)), safety_alert)
    webhooks = json.loads((ROOT/'src/server/contracts/openai_webhook_types_v1.json').read_text())
    assert webhooks['profile'] == 'q4t-openai-webhook-types-v1'
    definitions.update(graph_definitions(webhooks, 'webhook_'))
    definitions['webhook_query'] = obj(dict(limit=page_limit, after=dict(type='string')))
    for operation in ('create', 'update'):
        definitions['webhook_' + operation] = {'allOf': [definitions['webhook_' + operation], {
            'properties': {'name': dict(type='string', minLength=1, maxLength=256),
                           'url': dict(type='string', maxLength=2048, pattern='^https://'),
                           'event_types': dict(type='array', minItems=1)}}]}
    automations = json.loads((ROOT/'src/server/contracts/openai_automation_types_v1.json').read_text())
    assert automations['profile'] == 'q4t-openai-automation-types-v1'
    definitions.update(graph_definitions(automations, 'automation_'))
    for page, item in [('automation_list', 'automation_resource'),
                       ('automation_invocation_list', 'automation_invocation')]:
        definitions[page] = copy.deepcopy(definitions['agents_list'])
        definitions[page]['properties']['data']['items'] = ref(item)
    for name in ('query', 'invocation_query'):
        properties = dict(limit=page_limit, after=dict(type='string'),
                          order=descending_order)
        if name == 'query':
            properties['session_id'] = dict(type='string')
        definitions['automation_' + name] = obj(properties)
    vault_meta = dict(type='object', maxProperties=16,
                      propertyNames=dict(minLength=1, maxLength=64),
                      additionalProperties=dict(type='string', maxLength=512))
    vault_name = dict(type='string', description='Trim whitespace; resulting name must contain 1 to 256 UTF-8 bytes.')
    vault = dict(id=dict(type='string'), object=dict(const='vault'),
                 created_at=dict(type='integer'), name=nullable(vault_name), metadata=vault_meta)
    definitions['vault'] = obj(vault, vault.keys())
    definitions['vault_create'] = obj(dict(name=vault_name, metadata=nullable(vault_meta)))
    definitions['vault_update'] = obj(dict(name=nullable(vault_name), metadata=vault_meta))
    definitions['vault_deleted'] = obj(dict(id=dict(type='string'), object=dict(const='vault.deleted'),
                                           deleted=dict(const=True)), ['id', 'object', 'deleted'])
    skills = json.loads((ROOT/'src/server/contracts/openai_skill_types_v1.json').read_text())
    assert skills['profile'] == 'q4t-openai-skill-types-v1'
    definitions.update(graph_definitions(skills, 'skill_'))
    for name in ('query', 'version_query'):
        definitions['skill_' + str(skills['roots'][name])]['properties']['limit'] = dict(page_limit, minimum=0)
    for name in ('resource', 'version_resource'):
        definitions['skill_' + str(skills['roots'][name])]['properties']['created_at'] = dict(type='integer')
    encoded = dict(type='string', contentEncoding='base64', contentMediaType='application/zip')
    for name in ('skill_upload', 'skill_version_upload'):
        fields = {'files': {'oneOf':[encoded, dict(type='array', items=encoded, minItems=1, maxItems=1)]}}
        if name == 'skill_version_upload':
            fields['default'] = dict(type='boolean')
        definitions[name] = obj(fields, ['files'])
    containers = json.loads((ROOT/'src/server/contracts/openai_container_types_v1.json').read_text())
    assert containers['profile'] == 'q4t-openai-container-types-v1'
    definitions.update(graph_definitions(containers, 'container_'))
    for query in ('query', 'file_query'):
        fields = definitions['container_' + str(containers['roots'][query])]['properties']
        fields['limit'] = page_limit
        fields['order']['default'] = 'desc'
    for name, item in [('container_list','container_list_item'),('container_file_list','container_file_list_item')]:
        definitions[name] = copy.deepcopy(definitions['agents_list'])
        fields = definitions[name]['properties']
        fields['data']['items'] = ref(item)
        fields.update(first_id=dict(type='string'), last_id=dict(type='string'))
    expiry = obj({'anchor':dict(const='last_active_at'),
                  'minutes':dict(type='integer',minimum=1)}, ['anchor','minutes'])
    for shape in ('create','created','resource','list_item'):
        fields = definitions['container_' + str(containers['roots'][shape])]['properties']
        fields['expires_after'] = expiry
        for field in ('created_at','last_active_at'):
            if field in fields:
                fields[field] = dict(type='integer')
    for shape in ('file_created','file_resource','file_list_item'):
        fields = definitions['container_' + str(containers['roots'][shape])]['properties']
        fields.update(created_at=dict(type='integer'), bytes=dict(type='integer', minimum=0))
    definitions['container_file_create'] = obj({'file_id':dict(type='string')}, ['file_id'])
    for name, tag in [('container_deleted', 'container.deleted'),
                      ('container_file_deleted', 'container.file.deleted')]:
        definitions[name] = obj(dict(object=dict(const=tag), id=dict(type='string'),
                                     deleted=dict(type='boolean')), ['object','id','deleted'])
    assistants = json.loads((ROOT/'src/server/contracts/openai_assistant_types_v1.json').read_text())
    assert assistants['profile'] == 'q4t-openai-assistant-types-v1'
    definitions.update(graph_definitions(assistants, 'assistant_'))
    create_assistant = definitions['assistant_' + str(assistants['roots']['create'])]
    inline_search = {'properties': {'file_search': {'oneOf': [
        {'required': ['vector_store_ids']}, {'required': ['vector_stores']}]}}}
    create_assistant['properties']['tool_resources'].update(inline_search)
    thread_create = definitions['assistant_' + str(assistants['roots']['thread_create'])]
    thread_create['properties']['tool_resources'].update(inline_search)
    for name in ('query', 'message_query', 'run_list_query', 'step_list_query'):
        fields = definitions['assistant_' + str(assistants['roots'][name])]['properties']
        fields['limit'] = page_limit
        fields['order']['default'] = 'desc'
    for family in ('run', 'step'):
        definitions['assistant_'+family+'_list'] = copy.deepcopy(definitions['agents_list'])
        definitions['assistant_'+family+'_list']['properties']['data']['items'] = ref('assistant_'+family+'_resource')
    definitions['assistant_message_list'] = copy.deepcopy(definitions['agents_list'])
    definitions['assistant_message_list']['properties']['data']['items'] = ref('assistant_message_resource')
    definitions['assistant_list'] = copy.deepcopy(definitions['agents_list'])
    definitions['assistant_list']['properties']['data']['items'] = ref('assistant_resource')
    for family in ('assistant_list', 'assistant_message_list', 'assistant_run_list', 'assistant_step_list'):
        fields = definitions[family]['properties']
        fields.update(first_id=dict(type='string'), last_id=dict(type='string'))
    definitions['vault_list'] = copy.deepcopy(definitions['agents_list'])
    definitions['vault_list']['properties']['data']['items'] = ref('vault')
    definitions['vault_query'] = copy.deepcopy(definitions['agents_query'])
    status = dict(enum=['active', 'archived'])
    definitions['vault_query']['properties'].update(metadata=vault_meta, status=dict(
        anyOf=[status, dict(type='array', items=status)]))
    credential = json.loads((ROOT/'src/server/contracts/openai_vault_credential_types_v1.json').read_text())
    assert credential['profile'] == 'q4t-openai-vault-credential-types-v1'
    credential_types = graph_definitions(credential, 'credential_')
    for definition in credential_types.values():
        for name, field in definition.get('properties', {}).items():
            if name in {'token', 'access_token', 'refresh_token', 'client_secret', 'secret_value'}:
                field['writeOnly'] = True
    definitions.update(credential_types)
    definitions['credential_update'] = obj(dict(auth=ref('credential_auth_rotate'), metadata=vault_meta))
    definitions['credential_list'] = copy.deepcopy(definitions['agents_list'])
    definitions['credential_list']['properties']['data']['items'] = ref('credential_resource')
    environment = json.loads((ROOT/'src/server/contracts/openai_environment_types_v1.json').read_text())
    assert environment['profile'] == 'q4t-openai-environment-types-v1'
    environment_types = graph_definitions(environment, 'hosted_')
    for definition in environment_types.values():
        fields = definition.get('properties', {})
        for name, field in fields.items():
            if name in {'env', 'setup_commands', 'source', 'data'}:
                field['writeOnly'] = True
        if 'blocked_domains' in fields:
            definition['if'] = dict(required=['blocked_domains'], properties={
                'blocked_domains': dict(type='array', minItems=1)})
            definition['then'] = dict(properties={
                'access': dict(const='restricted'),
                'allowed_domains': dict(maxItems=0),
                'blocked_domains': dict(items=dict(type='string', minLength=1,
                                                    pattern=r'^[^*]+$'))})
    # The SDK moves this option to a header before sending the request.
    creation = 'hosted_' + str(environment['roots']['environment_create'])
    del environment_types[creation]['properties']['Idempotency-Key']
    for query, limit in [('environment_query', page_limit), ('file_query', nullable(page_limit))]:
        node = environment_types['hosted_' + str(environment['roots'][query])]
        node['properties']['limit'] = limit
    environment_list = environment_types['hosted_' + str(environment['roots']['environment_query'])]
    environment_list['properties']['order']['default'] = 'desc'
    definitions.update(environment_types)
    definitions['hosted_environment_list'] = copy.deepcopy(definitions['agents_list'])
    definitions['hosted_environment_list']['properties']['data']['items'] = ref('hosted_environment_resource')
    file_page = dict(object=dict(const='list'), data=dict(type='array', items=ref('hosted_file_resource')),
                     next=nullable(dict(type='string')), has_more=dict(type='boolean'))
    definitions['hosted_file_list'] = obj(file_page, file_page)
    definitions['hosted_template_list'] = copy.deepcopy(definitions['agents_list'])
    definitions['hosted_template_list']['properties']['data']['items'] = ref('hosted_template_resource')
    sessions = json.loads((ROOT/'src/server/contracts/openai_agent_session_types_v1.json').read_text())
    assert sessions['profile'] == 'q4t-openai-agent-session-types-v1'
    definitions.update(graph_definitions(sessions, 'agent_session_'))
    definitions['agent_session_event_query'] = obj(dict(
        include_obfuscation=dict(type='boolean', default=True)))
    for query in ('query', 'artifact_query', 'subagent_query'):
        node = definitions['agent_session_' + str(sessions['roots'][query])]
        node['properties']['limit'] = page_limit
        node['properties']['order']['default'] = 'desc'
    trace = dict(id=dict(type='string'), session_id=dict(type='string'),
                 object=dict(const='agent.session.trace'), created_at=dict(type='integer'),
                 otlp=dict(type='object', required=['resourceSpans'],
                           properties=dict(resourceSpans=dict(type='array', items=dict(type='object')))))
    definitions['agent_session_trace'] = obj(trace, trace.keys())
    for suffix, member in [('list', 'resource'), ('turn_list', 'turn'), ('item_list', 'item'),
                           ('trace_list', 'trace'), ('artifact_list', 'artifact'), ('subagent_list', 'subagent')]:
        page = copy.deepcopy(definitions['agents_list'])
        page['properties']['data']['items'] = ref('agent_session_' + member)
        definitions['agent_session_' + suffix] = page
    definitions['agent_session_event_batch'] = obj({
        'events': dict(type='array', minItems=1, maxItems=256,
                       items=ref('agent_session_input_event'))}, ['events'])
    decisions = json.loads((ROOT/'src/server/contracts/openai_decision_types_v1.json').read_text())
    assert decisions['profile'] == 'q4t-openai-decision-types-v1'
    definitions.update(graph_definitions(decisions, 'decisions_'))
    for index, node in enumerate(decisions['nodes']):
        fields = definitions['decisions_'+str(index)].get('properties', {})
        for name, upper in [('questions', 400), ('choices', 255), ('levels', 10)]:
            if name in fields:
                fields[name].update(minItems=1 if name == 'questions' else 2, maxItems=upper)
    from tools.serve_responses.files_contract import definitions as file_definitions
    definitions.update(file_definitions())
    from tools.serve_responses.uploads_contract import definitions as upload_definitions
    definitions.update(upload_definitions())
    from tools.serve_responses.batches_contract import definitions as batch_definitions
    definitions.update(batch_definitions())
    from tools.serve_responses.vector_stores_contract import definitions as vector_definitions
    definitions.update(vector_definitions())
    from tools.serve_responses.moderations_contract import definitions as moderation_definitions
    definitions.update(moderation_definitions())
    from tools.serve_responses.embeddings_contract import definitions as embedding_definitions
    definitions.update(embedding_definitions())
    from tools.serve_responses.transcriptions_contract import definitions as asr_definitions
    definitions.update(asr_definitions())
    from tools.serve_responses.web_contract import update_tools
    update_tools(definitions, graph)

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
    for types, event_root, prefix in ((graph, 'response_event', ''), (sessions, 'event', 'agent_session_')):
        for index in types['nodes'][types['roots'][event_root]]['variants']:
            node = types['nodes'][index]
            tag = next((types['nodes'][field['type']].get('value')
                        for field in node.get('fields', []) if field['name'] == 'type'), None)
            if isinstance(tag, str) and tag.endswith('.delta'):
                definitions[prefix+str(index)]['properties']['obfuscation'] = dict(type='string')

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
    # TypeScript string types do not express the frozen HTTP character limit.
    request['properties']['safety_identifier'] = dict(type=['string', 'null'], maxLength=128)
    request['properties']['reasoning']['default'] = dict(effort='medium')
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
            value['properties']['limit'] = page_limit
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
    from tools.serve_responses.uploads_contract import SOURCES as UPLOAD_SOURCES
    from tools.serve_responses.batches_contract import SDK_BATCHES_SHA256
    from tools.serve_responses.vector_stores_contract import SDK_SOURCES as VECTOR_SOURCES
    from tools.serve_responses.moderations_contract import SDK_MODERATIONS_SHA256
    from tools.serve_responses.embeddings_contract import SDK_SOURCES as EMBEDDING_SOURCES
    from tools.serve_responses.transcriptions_contract import SDK_SOURCES as ASR_SOURCES
    agents = json.loads((ROOT/'src/server/contracts/openai_multi_agent_types_v1.json').read_text())
    definitions.update(graph_definitions(agents, 'agent_'))
    configuration = definitions['agent_'+str(agents['roots']['configuration'])]
    configuration['properties']['max_concurrent_subagents'] = dict(type='integer', minimum=0)
    # The SDK selects these extra history variants with an HTTP header.
    # Copy just the selected request and keep each original array item union.
    for name in ('response_create', 'response_compact', 'input_tokens_request',
                 'input_items_list'):
        body = copy.deepcopy(root(name))
        field = 'data' if name == 'input_items_list' else 'input'
        original = body['properties'][field]
        target = definitions[original['$ref'].split('/')[-1]]
        variants = target.get('anyOf', [original])
        selected = []
        for variant in variants:
            value = definitions[variant['$ref'].split('/')[-1]]
            if value.get('type') == 'array':
                value = dict(value, items={'anyOf': [value['items'],
                    ref('agent_input_call'), ref('agent_input_result'), ref('agent_output_message')]})
            else:
                value = variant
            selected.append(value)
        body['properties'][field] = {'anyOf': selected}
        if name == 'response_create':
            body['properties']['multi_agent'] = nullable(ref('agent_configuration'))
        definitions['agent_history_'+name] = body
    selected_response = copy.deepcopy(root('response'))
    selected_items = dict(anyOf=[ref('output_item'), ref('agent_output_call'),
                                 ref('agent_output_result'), ref('agent_output_message')])
    selected_response['properties']['output'] = dict(type='array', items=selected_items)
    definitions['agent_response'] = selected_response
    events = []
    owned_events = {'response.output_item.added', 'response.output_item.done',
                    'response.content_part.added', 'response.content_part.done',
                    'response.output_text.delta', 'response.output_text.done',
                    'response.refusal.delta', 'response.refusal.done'}
    for index in graph['nodes'][graph['roots']['response_event']]['variants']:
        entry = definitions[str(index)]
        fields = entry.get('properties', {})
        tag = next((graph['nodes'][f['type']].get('value')
                    for f in graph['nodes'][index].get('fields', []) if f['name'] == 'type'), None)
        if 'response' not in fields and tag not in owned_events:
            events.append(ref(index))
            continue
        selected = copy.deepcopy(entry)
        fields = selected['properties']
        if 'response' in fields:
            fields['response'] = ref('agent_response')
        if tag in owned_events:
            fields['agent'] = nullable(obj({'agent_name': dict(type='string')}, ['agent_name']))
            if 'item' in fields:
                fields['item'] = selected_items
        key = 'agent_event_'+str(index)
        definitions[key] = selected
        events.append(ref(key))
    definitions['agent_response_event'] = dict(anyOf=events)
    return {'$schema': DRAFT, 'title': 'Q4T Chat, Responses, Conversations, Files, Uploads and Batches wire contract',
        '$defs': definitions, 'x-q4t': dict(profile='q4t-application-wire-v1',
            sdk_version=graph['sdk_version'], compiler_version=graph['compiler_version'],
            sources_sha256={**graph['sources_sha256'], **agents['sources_sha256'],
                            'src/resources/files.ts': SDK_FILES_SHA256, **UPLOAD_SOURCES,
                            'src/resources/batches.ts': SDK_BATCHES_SHA256,
                            'src/resources/moderations.ts': SDK_MODERATIONS_SHA256,
                            **EMBEDDING_SOURCES, **VECTOR_SOURCES, **ASR_SOURCES},
            license_sha256=graph['license_sha256'],
            compiler_sha256=graph['compiler_sha256'],
            semantics='Structural shapes include required planned capabilities. Shape validity does not promise that a model, tool or state executor is available. Check native planning and runtime capabilities; pending capabilities remain required implementation tasks.',
            adaptations=['SDK-added output_text is optional on wire',
                'FileObject.purpose includes evals already accepted by the SDK FilePurpose input; preserve the actual purpose',
                'Files multipart uses binary file and bracket-encoded expires_after fields; explicit native capacity and storage quotas remain separate',
                'Files HTTP list uses string first_id/last_id; native empty pages use empty strings and has_more=false, not nullable cursors from other list resources',
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
    from tools.serve_responses.mcp_tunnel_contract import add_openapi as add_tunnels
    add_tunnels(paths, response, error)
    from tools.serve_responses.batches_contract import add_openapi as add_batches
    add_batches(paths, response, error)
    from tools.serve_responses.vector_stores_contract import add_openapi as add_vectors
    add_vectors(paths, response, error)
    from tools.serve_responses.moderations_contract import add_openapi as add_moderations
    add_moderations(paths, response, error)
    from tools.serve_responses.embeddings_contract import add_openapi as add_embeddings
    add_embeddings(paths, response, error)
    from tools.serve_responses.transcriptions_contract import add_openapi as add_asr
    add_asr(paths, response, error)
    from tools.serve_responses.files_contract import add_openapi as add_files
    add_files(paths, response, error)
    from tools.serve_responses.uploads_contract import add_openapi as add_uploads
    add_uploads(paths, response, error)
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
            result.append(dict(name=key,
                **{'in': 'query'}, required=False, schema=rewrite(value),
                style='deepObject' if key == 'metadata' else 'form', explode=True))
            if key == 'include':
                result.append(dict(result[-1], name='include[]', **{'x-q4t-alias-for':'include'}))
        return result

    endpoints = [
        ('/v1/skills', 'get', None, 'skill_list', 'skill_query'),
        ('/v1/skills', 'post', 'skill_upload', 'skill_resource', None),
        ('/v1/skills/{skill_id}', 'get', None, 'skill_resource', None),
        ('/v1/skills/{skill_id}', 'post', 'skill_update', 'skill_resource', None),
        ('/v1/skills/{skill_id}', 'delete', None, 'skill_deleted', None),
        ('/v1/skills/{skill_id}/content', 'get', None, None, None),
        ('/v1/skills/{skill_id}/versions', 'post', 'skill_version_upload', 'skill_version_resource', None),
        ('/v1/skills/{skill_id}/versions', 'get', None, 'skill_version_list', 'skill_version_query'),
        ('/v1/skills/{skill_id}/versions/{version}', 'get', None, 'skill_version_resource', None),
        ('/v1/skills/{skill_id}/versions/{version}', 'delete', None, 'skill_version_deleted', None),
        ('/v1/skills/{skill_id}/versions/{version}/content', 'get', None, None, None),
        ('/v1/containers','get',None,'container_list','container_query'),
        ('/v1/containers','post','container_create','container_created',None),
        ('/v1/containers/{container_id}','get',None,'container_resource',None),
        ('/v1/containers/{container_id}','delete',None,'container_deleted',None),
        ('/v1/containers/{container_id}/files','post','container_file_create','container_file_created',None),
        ('/v1/containers/{container_id}/files','get',None,'container_file_list','container_file_query'),
        ('/v1/containers/{container_id}/files/{file_id}','get',None,'container_file_resource',None),
        ('/v1/containers/{container_id}/files/{file_id}','delete',None,'container_file_deleted',None),
        ('/v1/containers/{container_id}/files/{file_id}/content','get',None,None,None),
        ('/v1/vaults/{vault_id}/credentials', 'get', None, 'credential_list', 'vault_query'),
        ('/v1/vaults/{vault_id}/credentials', 'post', 'credential_create', 'credential_resource', None),
        ('/v1/vaults/{vault_id}/credentials/{credential_id}', 'get', None, 'credential_resource', None),
        ('/v1/vaults/{vault_id}/credentials/{credential_id}', 'post', 'credential_update', 'credential_resource', None),
        ('/v1/vaults/{vault_id}/credentials/{credential_id}', 'delete', None, 'credential_deleted', None),
        ('/v1/vaults', 'get', None, 'vault_list', 'vault_query'),
        ('/v1/vaults', 'post', 'vault_create', 'vault', None),
        ('/v1/vaults/{vault_id}', 'get', None, 'vault', None),
        ('/v1/vaults/{vault_id}', 'post', 'vault_update', 'vault', None),
        ('/v1/vaults/{vault_id}', 'delete', None, 'vault_deleted', None),
        ('/v1/threads/runs', 'post', 'assistant_thread_run_create', 'assistant_run_resource', None),
        ('/v1/threads/{thread_id}/runs', 'post', 'assistant_run_create', 'assistant_run_resource', 'assistant_step_query'),
        ('/v1/threads/{thread_id}/runs', 'get', None, 'assistant_run_list', 'assistant_run_list_query'),
        ('/v1/threads/{thread_id}/runs/{run_id}', 'get', None, 'assistant_run_resource', None),
        ('/v1/threads/{thread_id}/runs/{run_id}', 'post', 'assistant_run_update', 'assistant_run_resource', None),
        ('/v1/threads/{thread_id}/runs/{run_id}/cancel', 'post', None, 'assistant_run_resource', None),
        ('/v1/threads/{thread_id}/runs/{run_id}/submit_tool_outputs', 'post', 'assistant_run_submit', 'assistant_run_resource', None),
        ('/v1/threads/{thread_id}/runs/{run_id}/steps', 'get', None, 'assistant_step_list', 'assistant_step_list_query'),
        ('/v1/threads/{thread_id}/runs/{run_id}/steps/{step_id}', 'get', None, 'assistant_step_resource', 'assistant_step_query'),
        ('/v1/threads', 'post', 'assistant_thread_create', 'assistant_thread_resource', None),
        ('/v1/threads/{thread_id}', 'get', None, 'assistant_thread_resource', None),
        ('/v1/threads/{thread_id}', 'post', 'assistant_thread_update', 'assistant_thread_resource', None),
        ('/v1/threads/{thread_id}', 'delete', None, 'assistant_thread_deleted', None),
        ('/v1/threads/{thread_id}/messages', 'post', 'assistant_message_create', 'assistant_message_resource', None),
        ('/v1/threads/{thread_id}/messages', 'get', None, 'assistant_message_list', 'assistant_message_query'),
        ('/v1/threads/{thread_id}/messages/{message_id}', 'get', None, 'assistant_message_resource', None),
        ('/v1/threads/{thread_id}/messages/{message_id}', 'post', 'assistant_message_update', 'assistant_message_resource', None),
        ('/v1/threads/{thread_id}/messages/{message_id}', 'delete', None, 'assistant_message_deleted', None),
        ('/v1/assistants', 'post', 'assistant_create', 'assistant_resource', None),
        ('/v1/assistants', 'get', None, 'assistant_list', 'assistant_query'),
        ('/v1/assistants/{assistant_id}', 'get', None, 'assistant_resource', None),
        ('/v1/assistants/{assistant_id}', 'post', 'assistant_update', 'assistant_resource', None),
        ('/v1/assistants/{assistant_id}', 'delete', None, 'assistant_deleted', None),
        ('/v1/agents', 'post', 'agents_create', 'agents_resource', None),
        ('/v1/agents', 'get', None, 'agents_list', 'agents_query'),
        ('/v1/agents/{agent_id}', 'get', None, 'agents_resource', None),
        ('/v1/agents/{agent_id}', 'post', 'agents_update', 'agents_resource', None),
        ('/v1/agents/{agent_id}', 'delete', None, 'agents_deleted', None),
        ('/v1/agents/environments', 'get', None, 'hosted_environment_list', 'hosted_environment_query'),
        ('/v1/agents/environments', 'post', 'hosted_environment_create', 'hosted_environment_resource', None),
        ('/v1/agents/environments/{environment_id}', 'get', None, 'hosted_environment_resource', None),
        ('/v1/agents/environments/{environment_id}/files', 'get', None, 'hosted_file_list', 'hosted_file_query'),
        ('/v1/agents/environments/{environment_id}/files', 'post', 'hosted_file_create', 'hosted_file_resource', None),
        ('/v1/agents/environments/templates', 'get', None, 'hosted_template_list', 'agents_query'),
        ('/v1/agents/environments/templates', 'post', 'hosted_template_create', 'hosted_template_resource', None),
        ('/v1/agents/environments/templates/{environment_template_id}', 'get', None, 'hosted_template_resource', None),
        ('/v1/agents/environments/templates/{environment_template_id}', 'post', 'hosted_template_update', 'hosted_template_resource', None),
        ('/v1/agents/environments/templates/{environment_template_id}', 'delete', None, 'hosted_template_deleted', None),
        ('/v1/videos/characters', 'post', 'video_character_create', 'video_character', None),
        ('/v1/videos/characters/{character_id}', 'get', None, 'video_character', None),
        ('/v1/safety/alerts/{id}', 'get', None, 'safety_alert', None),
        ('/v1/safety/cases/{id}', 'get', None, 'safety_case', None),
        ('/v1/webhook_event_types', 'get', None, 'webhook_event_types', None),
        ('/v1/evals', 'get', None, 'eval_list', 'eval_query'),
        ('/v1/evals', 'post', 'eval_create', 'eval_eval', None),
        ('/v1/evals/{eval_id}', 'get', None, 'eval_eval', None),
        ('/v1/evals/{eval_id}', 'post', 'eval_update', 'eval_eval', None),
        ('/v1/evals/{eval_id}', 'delete', None, 'eval_deleted', None),
        ('/v1/evals/{eval_id}/runs', 'get', None, 'eval_run_list', 'eval_run_query'),
        ('/v1/evals/{eval_id}/runs', 'post', 'eval_run_create', 'eval_run', None),
        ('/v1/evals/{eval_id}/runs/{run_id}', 'get', None, 'eval_run', None),
        ('/v1/evals/{eval_id}/runs/{run_id}', 'delete', None, 'eval_run_deleted', None),
        ('/v1/evals/{eval_id}/runs/{run_id}/cancel', 'post', None, 'eval_run', None),
        ('/v1/evals/{eval_id}/runs/{run_id}/report', 'get', None, None, None),
        ('/v1/evals/{eval_id}/runs/{run_id}/output_items', 'get', None, 'eval_output_list', 'eval_output_query'),
        ('/v1/evals/{eval_id}/runs/{run_id}/output_items/{output_item_id}', 'get', None, 'eval_output_item', None),
        ('/v1/chatkit', 'post', 'chatkit_client_request', 'chatkit_client_result', None),
        ('/v1/chatkit/attachments', 'post', None, 'chatkit_document_attachment', None),
        ('/v1/chatkit/attachments/{session_id}/{file_id}', 'put', None, 'chatkit_document_attachment', None),
        ('/v1/chatkit/sessions', 'post', 'chatkit_create', 'chatkit_session', None),
        ('/v1/chatkit/sessions/{session_id}/cancel', 'post', None, 'chatkit_session', None),
        ('/v1/chatkit/threads', 'get', None, 'chatkit_threads', 'chatkit_thread_query'),
        ('/v1/chatkit/threads/{thread_id}', 'get', None, 'chatkit_thread', None),
        ('/v1/chatkit/threads/{thread_id}', 'delete', None, 'chatkit_deleted', None),
        ('/v1/chatkit/threads/{thread_id}/items', 'get', None, 'chatkit_items', 'chatkit_item_query'),
        ('/v1/webhook_endpoints', 'post', 'webhook_create', 'webhook_with_secret', None),
        ('/v1/webhook_endpoints', 'get', None, 'webhook_endpoint_list', 'webhook_query'),
        ('/v1/webhook_endpoints/{webhook_endpoint_id}', 'get', None, 'webhook_endpoint', None),
        ('/v1/webhook_endpoints/{webhook_endpoint_id}', 'post', 'webhook_update', 'webhook_endpoint', None),
        ('/v1/webhook_endpoints/{webhook_endpoint_id}', 'delete', None, 'webhook_deleted', None),
        ('/v1/webhook_endpoints/{webhook_endpoint_id}/rotate_secret', 'post', 'webhook_rotate', 'webhook_with_secret', None),
        ('/v1/webhook_endpoints/{webhook_endpoint_id}/test', 'post', 'webhook_test', 'webhook_test_result', None),
        ('/v1/agents/automations', 'post', 'automation_create', 'automation_resource', None),
        ('/v1/agents/automations', 'get', None, 'automation_list', 'automation_query'),
        ('/v1/agents/automations/{automation_id}', 'get', None, 'automation_resource', None),
        ('/v1/agents/automations/{automation_id}', 'post', 'automation_update', 'automation_resource', None),
        ('/v1/agents/automations/{automation_id}', 'delete', None, 'automation_deleted', None),
        ('/v1/agents/automations/{automation_id}/pause', 'post', None, 'automation_resource', None),
        ('/v1/agents/automations/{automation_id}/resume', 'post', None, 'automation_resource', None),
        ('/v1/agents/automations/{automation_id}/invocations', 'post', None, 'automation_invocation', None),
        ('/v1/agents/automations/{automation_id}/invocations', 'get', None, 'automation_invocation_list', 'automation_invocation_query'),
        ('/v1/agents/automations/{automation_id}/invocations/{invocation_id}', 'get', None, 'automation_invocation', None),
        ('/v1/agents/sessions', 'post', 'agent_session_create', 'agent_session_resource', None),
        ('/v1/agents/sessions', 'get', None, 'agent_session_list', 'agent_session_query'),
        ('/v1/agents/sessions/{session_id}', 'get', None, 'agent_session_resource', None),
        ('/v1/agents/sessions/{session_id}', 'post', 'agent_session_update', 'agent_session_resource', None),
        ('/v1/agents/sessions/{session_id}', 'delete', None, 'agent_session_deleted', None),
        ('/v1/agents/sessions/{session_id}/events', 'get', None, None, 'agent_session_event_query'),
        ('/v1/agents/sessions/{session_id}/events', 'post', 'agent_session_event_batch', None, None),
        ('/v1/agents/sessions/{session_id}/turns', 'get', None, 'agent_session_turn_list', 'agents_query'),
        ('/v1/agents/sessions/{session_id}/turns/{turn_id}', 'get', None, 'agent_session_turn', None),
        ('/v1/agents/sessions/{session_id}/items', 'get', None, 'agent_session_item_list', 'agents_query'),
        ('/v1/agents/sessions/{session_id}/subagents', 'get', None, 'agent_session_subagent_list', 'agent_session_subagent_query'),
        ('/v1/agents/sessions/{session_id}/subagents/{subagent_id}', 'get', None, 'agent_session_subagent', None),
        ('/v1/agents/sessions/{session_id}/subagents/{subagent_id}/items', 'get', None, 'agent_session_item_list', 'agent_session_subagent_query'),
        ('/v1/agents/sessions/{session_id}/subagents/{subagent_id}/turns', 'get', None, 'agent_session_turn_list', 'agent_session_subagent_query'),
        ('/v1/agents/sessions/{session_id}/subagents/{subagent_id}/turns/{turn_id}', 'get', None, 'agent_session_turn', None),
        ('/v1/agents/sessions/{session_id}/subagents/{subagent_id}/turns/{turn_id}/items', 'get', None, 'agent_session_item_list', 'agent_session_subagent_query'),
        ('/v1/agents/sessions/{session_id}/artifacts', 'get', None, 'agent_session_artifact_list', 'agent_session_artifact_query'),
        ('/v1/agents/sessions/{session_id}/artifacts/{artifact_id}', 'get', None, 'agent_session_artifact', None),
        ('/v1/agents/sessions/{session_id}/artifacts/{artifact_id}', 'delete', None, 'agent_session_artifact_deleted', None),
        ('/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content', 'get', None, None, None),
        ('/v1/agents/sessions/{session_id}/traces', 'get', None, 'agent_session_trace_list', 'agents_query'),
        ('/v1/agents/sessions/{session_id}/turns/{turn_id}/items', 'get', None, 'agent_session_item_list', 'agents_query'),
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
        if path.startswith('/v1/safety/'):
            parameters[0]['schema']['maxLength'] = 38 if result == 'safety_alert' else 128
        if query:
            parameters += query_fields(query)
        query_contract = query or 'no query'
        if path == '/v1/responses' or path.startswith('/v1/responses/'):
            parameters.append(dict(name='beta', **{'in': 'query'}, required=False,
                schema=dict(type='string', enum=['true']),
                description='Select the beta Responses endpoint.'))
            parameters.append(dict(name='OpenAI-Beta', **{'in': 'header'},
                required=False, schema=dict(type='string'),
                example='responses_multi_agent=v1',
                description='Comma-separated feature selectors. '
                            'responses_multi_agent=v1 selects hosted multi-agent execution and typed history.'))
            query_contract = query + ' with optional beta' if query else 'optional beta'
        event_stream = path.endswith('/events') and method == 'get'
        accepted = body == 'agent_session_event_batch'
        if path.endswith('/report') and path.startswith('/v1/evals/'):
            success = dict(description='Authenticated evaluation run report', content={
                'text/html': dict(schema=dict(type='string'))})
        elif path.startswith('/v1/skills/') and path.endswith('/content'):
            success = dict(description='Immutable ZIP bundle for the selected skill version', content={
                'application/zip': dict(schema=dict(type='string', format='binary')),
                'application/json': dict(schema=dict(type='string', contentEncoding='base64'))})
            parameters.append(dict(name='Accept', **{'in':'header'}, required=False, schema=dict(type='string')))
        elif path.startswith('/v1/containers/') and path.endswith('/content'):
            success = dict(description='Immutable container file bytes', content={
                'application/octet-stream': dict(schema=dict(type='string',format='binary'))})
        elif path.endswith('/artifacts/{artifact_id}/content'):
            success = dict(description='Immutable artifact contents', content={
                'application/octet-stream': dict(schema=dict(type='string', format='binary'))})
        elif event_stream or accepted:
            success = response({}, 'Live session events' if event_stream else 'Events accepted')
            success.pop('content')
        else:
            success = response(external(result))
        if body in ('assistant_run_create','assistant_thread_run_create','assistant_run_submit'):
            success['content']['text/event-stream'] = dict(schema=dict(type='string'),
                description='Assistants v2 named SSE resource and delta events, followed by done.',
                **{'x-q4t-event-schema': external('assistant_run_event')})
        if event_stream or body == 'agent_session_create':
            success.setdefault('content', {})['text/event-stream'] = dict(
                schema=dict(type='string'), **{'x-q4t-event-schema': external('agent_session_event')})
        if accepted:
            parameters.append(dict(name='Idempotency-Key', **{'in': 'header'}, required=False,
                schema=dict(type='string', minLength=1, maxLength=255, pattern=r'^[!-+\--~]+$')))

        if body == 'hosted_environment_create':
            parameters.append(dict(name='Idempotency-Key', **{'in': 'header'}, required=False,
                schema=dict(type='string', minLength=1, maxLength=256, pattern=r'^[ -~]+$')))
        success_code = '202' if accepted else '200'
        if body in {'eval_run_create', 'eval_create', 'agents_create', 'automation_create', 'agent_session_create', 'vault_create',
                    'credential_create', 'hosted_template_create',
                    'hosted_environment_create', 'hosted_file_create'}:
            success_code = '201'
        operation = dict(operationId=method+'_'+path.strip('/').replace('/', '_').replace('{', '').replace('}', ''),
            parameters=parameters, responses={success_code: success,
                'default': response(error, 'Native error; unknown/other-client identities return 404')},
            **{'x-q4t-query-contract': query_contract,
               'x-q4t-body-contract': body or 'empty body only'})
        if path.startswith(('/v1/assistants', '/v1/threads')):
            parameters.append(dict(name='OpenAI-Beta', **{'in':'header'}, required=True,
                schema=dict(type='string', pattern=r'(^|,)[ \t]*assistants=v2[ \t]*(,|$)'),
                example='assistants=v2'))
        if path.startswith(('/v1/agents', '/v1/vaults')):
            operation.setdefault('parameters', []).append(dict(name='OpenAI-Beta',
                **{'in':'header'}, required=True, schema=dict(type='string',
                    pattern=r'(^|,)[ \t]*agents=v1[ \t]*(,|$)'), example='agents=v1'))
        if body:
            optional = path in ('/v1/threads', '/v1/conversations', '/v1/responses/input_tokens', '/v1/vaults',
                                '/v1/agents/environments/templates')
            operation['requestBody'] = dict(required=not optional and body != 'webhook_rotate',
                content={'application/json': dict(schema=nullable(external(body))
                    if optional else external(body))})
        if body in ('skill_upload','skill_version_upload'):
            binary = dict(type='string', format='binary')
            fields = {'files': {'oneOf':[binary, dict(type='array', minItems=1, maxItems=500, items=binary)]}}
            if body == 'skill_version_upload':
                fields['default'] = dict(type='boolean')
            operation['requestBody']['content']['multipart/form-data'] = dict(schema=obj(fields, ['files']))
        if body == 'container_file_create':
            operation['requestBody']['content']['multipart/form-data'] = dict(schema={
                'type':'object','properties':{'file_id':{'type':'string'},'file':{'type':'string','format':'binary'}},
                'additionalProperties':False,'oneOf':[{'required':['file_id']},{'required':['file']}]})
        if body == 'agent_session_create':
            operation['description'] = (
                'Use a prepared hosted workspace or create one with inline settings. '
                'Sessions without an environment require initial input. '
                'Client function results are submitted to the session events endpoint. '
                'stream=true selects server-sent events.')
        if body in ('response_create', 'response_compact', 'input_tokens_request'):
            media = operation['requestBody']['content']['application/json']
            media['schema'] = dict(anyOf=[media['schema'], external('agent_history_'+body)])
            media['x-q4t-selector-schemas'] = {
                'default': external(body), 'responses_multi_agent=v1': external('agent_history_'+body)}
        if result == 'input_items_list':
            success['content']['application/json']['schema'] = external('agent_history_'+result)
        if result == 'response':
            success['content']['application/json']['schema'] = external('agent_response')
        if result == 'response' and not path.endswith('/cancel'):
            success['content']['text/event-stream'] = dict(schema=dict(type='string'),
                **{'x-q4t-event-schema': external('agent_response_event'),
                   'x-q4t-stream-contract': 'Native sequenced Responses events. No Chat DONE. Persisted original replay with starting_after; background requires original stream=true. Disconnect does not cancel a background job. Cancelled resource retains partial items and ends original events without a fabricated completed/cancelled event.'})
        if path.endswith('/cancel'):
            operation['description'] = 'Idempotent native background cancellation, empty body, 200 real resource. Independent bounded cancellation admission.'
        if path == '/v1/chatkit':
            operation['description'] = 'Self-hosted browser endpoint. Requires a session client secret. Message submissions stream ChatKit events, history operations return JSON.'
            event = external('chatkit_browser_output_event')
            success['content']['text/event-stream'] = dict(schema=dict(type='string'),
                **{'x-q4t-event-schema': event})
            operation['x-q4t-result-by-request-type'] = {
                name: external(result) for name, result in CHATKIT_BROWSER_JSON.items()}
            operation['x-q4t-event-by-request-type'] = dict.fromkeys(CHATKIT_BROWSER_SSE, event)
        if body == 'video_character_create':
            operation['requestBody'] = dict(required=True, content={'multipart/form-data':
                dict(schema=external('video_character_create'))})
        if path == '/v1/chatkit/attachments':
            operation['description'] = 'Upload a document with a ChatKit session secret. The session must enable files. The returned ID can be used in that session.'
            operation['x-q4t-body-contract'] = 'one multipart file'
            operation['requestBody'] = dict(required=True, content={'multipart/form-data':dict(
                schema=obj(dict(file=dict(type='string',format='binary')), ['file']))})
        elif path == '/v1/chatkit/attachments/{session_id}/{file_id}':
            patterns = {'session_id': '^cksess_[0-9a-f]{64}$', 'file_id': '^file_[0-9a-f]{32}$'}
            for param in operation['parameters']:
                if param['in'] == 'path':
                    param['schema']['pattern'] = patterns[param['name']]
            operation['description'] = 'Complete the reserved document with its upload credential and the media type returned by attachments.create.'
            operation['x-q4t-body-contract'] = 'raw bytes of the reserved size and media type'
            operation['requestBody'] = dict(required=True, content={'*/*': dict(
                schema=dict(type='string', format='binary'))})
        elif path.startswith('/v1/chatkit/'):
            operation.setdefault('parameters', []).append(dict(name='OpenAI-Beta',
                **{'in':'header'}, required=True, schema=dict(type='string', const='chatkit_beta=v1')))
            if path.endswith('/cancel'):
                operation['description'] = 'Cancel the session and revoke future use of its client secret.'
        if path.startswith('/v1/prompts'):
            operation['description'] = 'q4t native Prompt registry, scoped to authenticated client. Immutable versions; revision-conditional append/default selection; durable effective request snapshots. Does not access hosted prompt accounts.'
        paths.setdefault(path, {})[method] = operation


def standalone_openapi(spec):
    """Make the shipped OpenAPI self-contained, without fetching schemas."""
    components = {}
    references = set()

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
                    references.add(result[key])
                else:
                    result[key] = rewrite(child, owner)
            return result
        if isinstance(value, list):
            return [rewrite(child, owner) for child in value]
        return value

    bundled = rewrite(spec)
    bundled['components'] = dict(schemas=components)
    for pointer in sorted(references):
        node = bundled
        try:
            for part in pointer[2:].split('/'):
                key = part.replace('~1', '/').replace('~0', '~')
                node = node[int(key)] if isinstance(node, list) else node[key]
        except (KeyError, IndexError, ValueError, TypeError) as error:
            raise ValueError('unresolved bundled reference '+pointer) from error
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
