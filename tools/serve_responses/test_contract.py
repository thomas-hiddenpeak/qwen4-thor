"""Targeted declarations; archived failures stay failures, new wire must pass."""
import copy
import hashlib
import json
import os
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol import build_contract as api
from tools.serve_responses import build_contract as app

ROOT = app.ROOT
SCHEMA = json.loads((ROOT/'docs/serve-application-v1.schema.json').read_text())


def resolve(value):
    while isinstance(value, dict) and '$ref' in value:
        value = SCHEMA['$defs'][value['$ref'].split('/')[-1]]
    return value


def errors(value, name):
    selection = app.ref(name)
    if name == 'response_event':
        variants = resolve(selection)['anyOf']
        # Select the exact tagged event for useful errors, not the many
        # unrelated union branches. Unknown event tags still fail the union.
        for variant in variants:
            body = resolve(variant)
            tag = resolve(body.get('properties', {}).get('type', {}))
            if tag.get('const') == value.get('type'):
                selection = variant
                break
    return list(Draft202012Validator(dict(selection, **{'$defs': SCHEMA['$defs']})).iter_errors(value))


def kind(value, route=''):
    if not isinstance(value, dict):
        return None
    if 'sequence_number' in value:
        return 'response_event'
    name = value.get('object')
    if name == 'response.compaction':
        return 'compacted_response'
    if name in ('response', 'conversation'):
        return name
    if name in ('response.deleted', 'conversation.deleted'):
        return name.replace('.', '_')
    if name == 'list':
        return 'conversation_items' if '/conversations' in route else 'input_items_list'
    if 'type' in value and '/conversations' in route:
        return 'conversation_item'
    return None


def wire_values(root):
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        if path.suffix == '.wire':
            text = path.read_text()
            try:
                value = json.loads(text)
                name = kind(value)
                if name:
                    yield path, name, value
            except json.JSONDecodeError:
                for line in text.splitlines():
                    if line.startswith('data: {'):
                        value = json.loads(line[6:])
                        name = kind(value)
                        if name:
                            yield path, name, value
        if path.name in ('wire.jsonl', 'requests.jsonl', 'events.jsonl'):
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if isinstance(row, list):
                    for value in row:
                        yield path, 'response_event', value
                    continue
                body = row.get('response', row.get('body'))
                if not isinstance(body, str) or not body.startswith('{'):
                    continue
                value = json.loads(body)
                name = kind(value, row.get('url', row.get('target', '')))
                if name:
                    yield path, name, value


class ApplicationContract(unittest.TestCase):
    def test_empty_batch_and_vector_list_cursor_contracts(self):
        page = dict(object='list', data=[], has_more=False)
        for name in ('batch_list', 'vector_store_list', 'vector_file_list'):
            with self.subTest(name=name):
                self.assertFalse(errors(dict(page, first_id='', last_id=''), name))
                self.assertEqual(bool(errors(page, name)), name != 'batch_list')
                for field in ('first_id', 'last_id'):
                    for invalid in (None, 42, False):
                        cursors = dict(first_id='', last_id='')
                        cursors[field] = invalid
                        self.assertTrue(errors(dict(page, **cursors), name))

    def test_empty_updates_preserve_the_object_and_creation_contracts(self):
        spec = app.standalone_openapi(api.openapi())
        paths = spec['paths']
        for path in ('/v1/agents/{agent_id}', '/v1/agents/sessions/{session_id}',
                     '/v1/agents/environments/templates/{environment_template_id}',
                     '/v1/webhook_endpoints/{webhook_endpoint_id}'):
            with self.subTest(path=path):
                body = paths[path]['post']['requestBody']
                self.assertFalse(body['required'])
                schema = body['content']['application/json']['schema']
                validate = Draft202012Validator({'components': spec['components'], **schema})
                self.assertTrue(validate.is_valid({}))
                self.assertFalse(validate.is_valid(None))
        for path in ('/v1/agents', '/v1/agents/sessions', '/v1/webhook_endpoints',
                     '/v1/agents/sessions/{session_id}/events'):
            self.assertTrue(paths[path]['post']['requestBody']['required'])

    def test_image_context_policy_stable_and_beta_requests(self):
        for name in ('response_create', 'response_compact',
                     'agent_history_response_create', 'agent_history_response_compact'):
            fields = resolve(SCHEMA['$defs'][name])['properties']
            image = resolve(fields['image'])
            self.assertIs(image['additionalProperties'], False)
            self.assertEqual(image['properties']['context_compression']['enum'],
                             ['auto', 'enabled', 'disabled'])
            self.assertNotIn('context_compression', image.get('required', []))
        for policy in ('auto', 'enabled', 'disabled'):
            self.assertFalse(errors({'context_compression': policy}, 'image_options'))
        for bad in (None, {'context_compression': None}, {'context_compression': True}, {'other': 1}):
            self.assertTrue(errors(bad, 'image_options'))
        self.assertTrue(errors({'context_compression': 'auto'}, 'resolved_image_options'))
        self.assertTrue(errors({}, 'resolved_image_options'))
        for name in ('response', 'compacted_response', 'agent_response'):
            image = resolve(SCHEMA['$defs'][name])['properties']['image']
            self.assertEqual(image, {'$ref': '#/$defs/resolved_image_options'})

    def report(self, name, value):
        output = os.environ.get('Q4T_TEST_OUTPUT_DIR')
        if output:
            root = Path(output)
            root.mkdir(parents=True, exist_ok=True)
            (root/(name+'.json')).write_text(json.dumps(value, indent=2)+'\n')

    def audit(self, root, legacy=False):
        checked, rejected, sources = 0, [], {}
        for path, name, value in wire_values(root):
            checked += 1
            sources[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
            failures = errors(value, name)
            if not failures:
                continue
            descriptions = [dict(path=list(e.absolute_path), validator=e.validator,
                                  message=e.message[:240]) for e in failures]
            rejected.append(dict(file=str(path.relative_to(ROOT)), kind=name, errors=descriptions))
            if not legacy:
                self.fail(str(rejected[-1]))
            # Preserve and identify only the actually discovered omissions.
            # This does not convert historical wire into compatible wire.
            for error in failures:
                missing_choice = error.validator == 'required' and error.message == "'tool_choice' is a required property"
                def leaves(item):
                    return [item] if not item.context else [leaf for child in item.context for leaf in leaves(child)]
                missing_cache = error.validator == 'anyOf' and list(error.absolute_path)[-1:] == ['usage'] and any(
                    e.validator == 'required' and e.message == "'cache_write_tokens' is a required property"
                    for e in leaves(error))
                missing_logs = name == 'response_event' and value.get('type') in (
                    'response.output_text.delta', 'response.output_text.done') and error.validator == 'required' and error.message == "'logprobs' is a required property"
                self.assertTrue(missing_choice or missing_cache or missing_logs, str(rejected[-1]))
        self.assertGreater(checked, 0)
        self.report(root.name+'-wire-review', dict(checked=checked, rejected=rejected,
            sources_sha256=sources, legacy_nonconforming_not_reaccepted=legacy))
        return checked, rejected

    def test_sdk_graph_and_closed_native_requests(self):
        Draft202012Validator.check_schema(SCHEMA)
        fields = {row['field'] for row in json.loads((ROOT/'docs/serve-responses-fields-v1.json').read_text())['fields']}
        self.assertEqual(set(resolve(app.ref('response_create'))['properties']),
                         fields | {'request_id', 'cancel_token', 'request_timeout_ms', 'seed', 'personality'})
        for request in [dict(input='hi'), dict(input='hi', background=True, store=False),
                        dict(input='hi', tool_choice='none', max_output_tokens=1)]:
            self.assertFalse(errors(request, 'response_create'))
        for change in [dict(unknown=1), dict(max_output_tokens=0), dict(top_p=2),
                       dict(top_logprobs=21), dict(metadata={'a': 'x'*513}),
                       dict(previous_response_id='resp_a', conversation='conv_b'),
                       dict(stream_options={}), dict(request_id='abc')]:
            self.assertTrue(errors(dict(input='hi', **change), 'response_create'))
        self.assertTrue(errors(dict(items=[], include=[]), 'conversation_items_create'))
        self.assertTrue(errors(dict(limit=101), 'input_items_query'))
        self.assertTrue(errors(dict(starting_after=1), 'response_retrieve'))

    def test_chatkit_custom_action_request_shapes(self):
        for name in ('threads.custom_action', 'threads.sync_custom_action'):
            def valid(params):
                return not errors(dict(type=name, params=params), 'chatkit_client_request')
            for payload in (None, [7], 'release', {'field':'revision'}):
                self.assertTrue(valid(dict(thread_id='cthr_a', action=dict(type='lookup', payload=payload))))
            self.assertTrue(valid(dict(thread_id='cthr_a', item_id=None, action=dict(type='show'))))
            self.assertTrue(valid(dict(thread_id='cthr_a', item_id='item_1', action=dict(type='show'))))
            for params in [dict(action={'type':'read'}), dict(thread_id='t', action={}),
                           dict(thread_id='t', item_id=19, action={'type':'x'})]:
                self.assertFalse(valid(params))

    def test_chatkit_browser_submits_structured_answers(self):
        def validate(value, **fields):
            params = dict(thread_id='cthr_a', item_id='fc_a', input=value, **fields)
            return not errors(dict(type='threads.add_structured_input', params=params),
                              'chatkit_client_request')
        for data in [{}, {'status':'skipped'}, {'answers':{'key':{'values':['a','b']}}},
                     {'answers':{'key':{'skipped':True}}}]:
            self.assertTrue(validate(data), data)
        for data in [{'status':'pending'}, {'answers':{'q':{'values':'text'}}},
                     {'answers':{'q':{'values':[3]}}}, None]:
            self.assertFalse(validate(data), data)
        for key in ['thread_id','item_id','input']:
            params = dict(thread_id='cthr_a', item_id='fc_a', input={})
            del params[key]
            self.assertTrue(errors(dict(type='threads.add_structured_input', params=params),
                                   'chatkit_client_request'))

    def test_compact_has_its_own_closed_stateless_contract(self):
        for request in [dict(model=None), dict(model='cpu', input='hi'),
                        dict(model='cpu', previous_response_id='resp_previous')]:
            self.assertFalse(errors(request, 'response_compact'))
        for field in ('stream', 'store', 'background', 'max_output_tokens', 'tools'):
            self.assertTrue(errors(dict(model='cpu', **{field: None}), 'response_compact'))
        self.assertTrue(errors(dict(input='hi'), 'response_compact'))
        self.assertNotIn('text/event-stream', api.openapi()['paths'][
            '/v1/responses/compact']['post']['responses']['200']['content'])

    def test_obfuscation_is_closed_to_documented_delta_events(self):
        event = dict(type='response.compaction.compacting', item_id='cmp_a',
                     output_index=0, sequence_number=0)
        self.assertFalse(errors(event, 'response_event'))
        self.assertTrue(errors(dict(event, obfuscation='padding'), 'response_event'))
        delta = dict(type='response.output_text.delta', item_id='msg_a',
                     output_index=0, content_index=0, sequence_number=1,
                     delta='42', logprobs=[], obfuscation='padding')
        self.assertFalse(errors(delta, 'response_event'))
        self.assertTrue(errors(dict(delta, obfuscation=1), 'response_event'))

    def test_agent_list_limits_match_native_integer_boundaries(self):
        spec = api.openapi()
        for route, nullable in [('/v1/agents/environments', False),
                                ('/v1/agents/environments/{environment_id}/files', True),
                                ('/v1/agents/sessions', False)]:
            with self.subTest(route=route):
                query = spec['paths'][route]['get']['parameters']
                schema = next(p['schema'] for p in query if p['name'] == 'limit')
                validator = Draft202012Validator(schema)
                self.assertTrue(all(validator.is_valid(n) for n in (1, 20, 100)))
                self.assertTrue(all(not validator.is_valid(n) for n in (0, 1.5, 101, '20', True)))
                self.assertEqual(validator.is_valid(None), nullable)

    def test_agent_session_cache_and_stream_controls(self):
        base = dict(environment=dict(type='none'), input='42')
        for stream in ({}, {'stream': False}, {'stream': True}):
            for options in ({}, {'include_obfuscation': False},
                            {'include_obfuscation': 'false'}, None):
                with self.subTest(stream=stream, options=options):
                    valid = stream.get('stream') and options in ({}, {'include_obfuscation': False})
                    self.assertEqual(not errors(dict(base, **stream, stream_options=options),
                                                'agent_session_create'), bool(valid))
        for name, fields in [('agent_session_create', base), ('agent_session_update', {})]:
            for value in (None, {}, {'ttl': '30m'}, {'ttl': '12h'}, {'ttl': None},
                          {'ttl': '24h'}, {'ttl': 1800}, {'other': 1}):
                with self.subTest(name=name, value=value):
                    self.assertEqual(not errors(dict(fields, prompt_cache_options=value), name),
                                     value in (None, {}, {'ttl': '30m'}, {'ttl': '12h'}))
        self.assertTrue(errors({'stream_options': {}}, 'agent_session_update'))
        create = resolve(SCHEMA['$defs']['agent_session_create'])['properties']
        stream = resolve(create['stream_options'])['properties']['include_obfuscation']
        self.assertIs(stream['default'], True)
        def cache_record(properties):
            variants = resolve(properties['prompt_cache_options'])['anyOf']
            return next(resolve(v) for v in variants if resolve(v).get('type') == 'object')
        request = cache_record(create)
        public = cache_record(resolve(SCHEMA['$defs']['agent_session_resource'])['properties'])
        self.assertEqual(request['properties']['ttl']['default'], '30m')
        self.assertNotIn('ttl', request.get('required', []))
        self.assertIn('ttl', public['required'])
        turn = resolve(SCHEMA['$defs']['agent_session_turn'])
        field = resolve(turn['properties']['prompt_cache_options'])['anyOf']
        self.assertEqual(cache_record(turn['properties']), public)
        self.assertTrue(any(resolve(v).get('type') == 'null' for v in field))
        self.assertNotIn('prompt_cache_options', turn['required'])


    def test_chatkit_and_eval_page_sizes_match_the_native_query_parser(self):
        bundle = app.standalone_openapi(api.openapi())
        paths = bundle['paths']
        cases = {
            '/v1/chatkit/threads': 'chatkit_thread_query',
            '/v1/chatkit/threads/{thread_id}/items': 'chatkit_item_query',
            '/v1/evals': 'eval_query',
            '/v1/evals/{eval_id}/runs': 'eval_run_query',
            '/v1/evals/{eval_id}/runs/{run_id}/output_items': 'eval_output_query',
        }
        for route, definition in cases.items():
            with self.subTest(route=route):
                params = paths[route]['get']['parameters']
                param = next(item for item in params if item['name'] == 'limit')
                self.assertFalse(param['required'])
                self.assertEqual(param['schema']['default'], 20)
                validator = Draft202012Validator(dict(components=bundle['components'], **param['schema']))
                self.assertFalse(errors({}, definition))
                for value in (-1, 0, 1, 1.5, 20, 100, 101, '20', True, None):
                    accepted = type(value) is int and value >= 0 and (
                        not route.startswith('/v1/chatkit/') or value <= 100)
                    self.assertEqual(validator.is_valid(value), accepted)
                    self.assertEqual(not errors({'limit': value}, definition), accepted)

    def test_chatkit_browser_pagination_fields(self):
        for operation in ('threads.list', 'items.list'):
            base = {'thread_id': 'cthr_own'} if operation == 'items.list' else {}
            def accepted(**kwargs):
                return not errors(dict(type=operation, params=dict(base, **kwargs)), 'chatkit_client_request')
            self.assertTrue(accepted() and accepted(order='asc', after=None))
            for value in (0, None, 1, 20, 100):
                self.assertTrue(accepted(limit=value))
            for value in (-1, False, 1.5, 101, '20'):
                self.assertFalse(accepted(limit=value))
            self.assertFalse(accepted(order=None))
        self.assertTrue(errors(dict(type='items.list', params={}), 'chatkit_client_request'))

    def test_chatkit_retry_selects_a_thread_and_user_item(self):
        def accepted(params):
            return not errors(dict(type='threads.retry_after_item', params=params),
                              'chatkit_client_request')
        self.assertTrue(accepted(dict(thread_id='cthr_a', item_id='item_a')))
        for params in ({}, {'thread_id': 'cthr_a'}, {'item_id': 'item_a'},
                       {'thread_id': 'cthr_a', 'item_id': None}):
            self.assertFalse(accepted(params))

    def test_application_list_defaults_survive_bundling(self):
        paths = app.standalone_openapi(api.openapi())['paths']
        session = '/v1/agents/sessions/{session_id}'
        for route, key, expected in (
            ('/v1/agents', 'order', 'desc'),
            ('/v1/agents/automations/{automation_id}/invocations', 'order', 'desc'),
            ('/v1/agents/environments', 'order', 'desc'),
            (session + '/artifacts', 'order', 'desc'),
            (session + '/subagents/{subagent_id}/items', 'order', 'desc'),
            ('/v1/assistants', 'order', 'desc'),
            ('/v1/threads/{thread_id}/runs/{run_id}/steps', 'order', 'desc'),
            ('/v1/containers/{container_id}/files', 'order', 'desc'),
            ('/v1/vaults/{vault_id}/credentials', 'order', 'desc'),
            ('/v1/evals', 'order', 'asc'),
            ('/v1/evals', 'order_by', 'created_at'),
            ('/v1/evals/{eval_id}/runs/{run_id}/output_items', 'order', 'asc'),
            ('/v1/conversations/{conversation_id}/items', 'limit', 20),
            ('/v1/responses/{response_id}/input_items', 'limit', 20),
        ):
            with self.subTest(route=route, key=key):
                param = next(p for p in paths[route]['get']['parameters'] if p['name'] == key)
                self.assertFalse(param['required'])
                self.assertEqual(param['schema']['default'], expected)

    def test_skill_content_representations_uploads_and_zero_page(self):
        paths = api.openapi()['paths']
        for route, query in [('/v1/skills', 'skill_query'),
                             ('/v1/skills/{skill_id}/versions', 'skill_version_query')]:
            limit = next(p['schema'] for p in paths[route]['get']['parameters'] if p['name'] == 'limit')
            self.assertTrue(Draft202012Validator(limit).is_valid(0))
            for value in (0, 100):
                self.assertFalse(errors({'limit':value},query))
            for value in (-1, 101, 1.5, '0', True):
                self.assertTrue(errors({'limit':value},query))
            form = paths[route]['post']['requestBody']['content']['multipart/form-data']['schema']
            valid = Draft202012Validator(form).is_valid
            self.assertTrue(valid({'files':['root/SKILL.md','root/data']}) and valid({'files':'zip'}))
            self.assertFalse(valid({'files':[]}) or valid({'files':['x']*501}))
            self.assertEqual(valid({'files':'zip','default':True}),route.endswith('/versions'))
        for route in ('/v1/skills/{skill_id}/content','/v1/skills/{skill_id}/versions/{version}/content'):
            content = paths[route]['get']['responses']['200']['content']
            self.assertEqual(set(content), {'application/json','application/zip'})
            self.assertEqual(content['application/json']['schema']['contentEncoding'],'base64')
        for name in ('skill_list','skill_version_list'):
            self.assertFalse(errors(dict(object='list',data=[],first_id=None,last_id=None,has_more=False),name))

    def test_container_expiry_pages_file_forms_and_deletion(self):
        for minutes in (0, 1.5, '2', True, 1, 20):
            value = dict(name='release', expires_after=dict(anchor='last_active_at', minutes=minutes))
            self.assertEqual(not errors(value, 'container_create'), type(minutes) is int and minutes > 0)
        for name in ('container_list', 'container_file_list'):
            page = dict(object='list', data=[], has_more=False, first_id='', last_id='')
            self.assertFalse(errors(page, name))
            self.assertTrue(errors(dict(page, first_id=None), name))
        for name, tag in [('container_deleted','container.deleted'),
                          ('container_file_deleted','container.file.deleted')]:
            self.assertFalse(errors(dict(id='record', object=tag, deleted=True), name))
            self.assertTrue(errors({}, name))
        body = api.openapi()['paths']['/v1/containers/{container_id}/files']['post']['requestBody']['content']
        valid = Draft202012Validator(body['multipart/form-data']['schema']).is_valid
        self.assertTrue(valid({'file':'bytes'}) and valid({'file_id':'file_a'}))
        self.assertFalse(valid({}) or valid({'file':'bytes','file_id':'file_a'}))
        self.assertTrue(errors({'file':'bytes'}, 'container_file_create'))

    def test_assistant_registry_tool_resources_and_field_bounds(self):
        valid = dict(model='qwen', name='业'*256, temperature=0, top_p=1)
        self.assertFalse(errors(valid, 'assistant_create'))
        for extra in [dict(name='业'*257),dict(temperature=2.01),dict(top_p=-0.01),
                      dict(tools=[{'type':'file_search'}]*129),
                      dict(tool_resources={'code_interpreter':{'file_ids':['file_1']*21}}),
                      dict(tool_resources={'file_search':{}}),
                      dict(tool_resources={'file_search':{'vector_store_ids':['vs_1','vs_2']}}),
                      dict(tool_resources={'file_search':{'vector_store_ids':[], 'vector_stores':[]}})]:
            with self.subTest(field=list(extra)):
                self.assertTrue(errors(dict(valid, **extra), 'assistant_create'))
        self.assertFalse(errors(dict(tool_resources={'file_search':{}}), 'assistant_update'))
        self.assertFalse(errors(dict(valid, tool_resources={'file_search':{
            'vector_stores':[{'file_ids':[]}]}}), 'assistant_create'))

    def test_thread_and_message_schema_matches_sdk_body_and_path(self):
        message = dict(role='user', content=[dict(type='text', text='release')])
        self.assertFalse(errors(message, 'assistant_message_create'))
        self.assertFalse(errors(dict(messages=[message]), 'assistant_thread_create'))
        self.assertFalse(errors(dict(metadata=None), 'assistant_message_update'))
        for bad in [dict(message, content=[]), dict(message, role='system'),
                    dict(message, attachments=[{}]), dict(message, metadata={'x':'长'*513})]:
            self.assertTrue(errors(bad, 'assistant_message_create'))
            self.assertTrue(errors(dict(messages=[bad]), 'assistant_thread_create'))
        self.assertTrue(errors(dict(thread_id='thread_1'), 'assistant_message_update'))
        self.assertTrue(errors(dict(tool_resources=dict(file_search={})), 'assistant_thread_create'))
        spec = api.openapi()['paths']
        self.assertFalse(spec['/v1/threads']['post']['requestBody']['required'])
        page = spec['/v1/threads/{thread_id}/messages']['get']
        query = {arg['name']:arg for arg in page['parameters'] if arg['in']=='query'}
        self.assertEqual(set(query), {'after','before','limit','order','run_id'})
        self.assertEqual(query['limit']['schema']['maximum'], 100)

    def test_assistant_run_tokens_tools_queries_and_step_parent(self):
        create = dict(assistant_id='asst_q4t', max_prompt_tokens=256,
                      max_completion_tokens=257, tools=[dict(type='file_search')]*20)
        self.assertFalse(errors(create, 'assistant_run_create'))
        self.assertFalse(errors(dict(create, thread={'messages': []}), 'assistant_thread_run_create'))
        for extra in [dict(max_prompt_tokens=255), dict(max_completion_tokens=256.5),
                      dict(tools=[dict(type='file_search')]*21), dict(include=[])]:
            self.assertTrue(errors(dict(create, **extra), 'assistant_run_create'))
        self.assertFalse(errors(dict(tool_outputs=[dict(tool_call_id='call_a', output='460')]),
                                'assistant_run_submit'))
        self.assertTrue(errors(dict(thread_id='thread_a', run_id='run_a'), 'assistant_step_query'))
        key = 'step_details.tool_calls[*].file_search.results[*].content'
        self.assertFalse(errors(dict(include=[key]), 'assistant_step_query'))
        self.assertTrue(errors(dict(include=['unknown']), 'assistant_step_query'))

    def test_archived_real_business_wire(self):
        checked, rejected = self.audit(ROOT/'docs/evidence/serve-responses-business-20261109/business-01', legacy=True)
        self.assertGreater(checked, 20)
        self.assertTrue(rejected)  # Original accepted subset had real SDK gaps.

    def test_archived_background_and_conversations(self):
        self.audit(ROOT/'docs/evidence/serve-responses-recovery-20261109/background-sdk-01/work', legacy=True)
        _, rejected = self.audit(ROOT/'docs/evidence/serve-conversations-20261109/sdk-conversations-01/native')
        self.assertFalse(rejected)

    def test_current_native_wire_is_strict(self):
        checked = 0
        for path in os.environ['Q4T_CURRENT_APPLICATION_WIRE'].split(os.pathsep):
            count, rejected = self.audit(Path(path))
            checked += count
            self.assertFalse(rejected)
        self.assertGreater(checked, 20)

    def test_openapi_routes_queries_and_no_external_reference_bundle(self):
        spec = api.openapi()
        self.assertEqual(api.runtime_routes(spec), set(json.loads((ROOT/'tools/serve_audit/inventory.json').read_text())['routes']))
        cancel = spec['paths']['/v1/responses/{response_id}/cancel']['post']
        self.assertIn('200', cancel['responses'])
        self.assertNotIn('requestBody', cancel)
        self.assertEqual([p['name'] for p in spec['paths']['/v1/responses/{response_id}']['get']['parameters']],
            ['response_id', 'include', 'include[]', 'include_obfuscation', 'starting_after', 'stream',
             'beta', 'OpenAI-Beta'])
        subscription = spec['paths']['/v1/agents/sessions/{session_id}/events']['get']
        self.assertEqual([p['schema'] for p in subscription['parameters']
                          if p['in'] == 'query' and p['name'] == 'include_obfuscation'],
                         [dict(type='boolean', default=True)])
        self.assertNotIn('text/event-stream', cancel['responses']['200']['content'])
        bundled = app.standalone_openapi(spec)
        def visit(value):
            if isinstance(value, dict):
                if isinstance(value.get('$ref'), str):
                    self.assertTrue(value['$ref'].startswith('#/'))
                    target = bundled
                    for component in value['$ref'][2:].split('/'):
                        target = target[component.replace('~1', '/').replace('~0', '~')]
                    self.assertIsInstance(target, (dict, bool))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(bundled)
        self.assertEqual(bundled, json.loads((ROOT/'docs/serve-openapi-bundle-v1.json').read_text()))

    def test_current_background_config_and_negative_bounds(self):
        schema = json.loads((ROOT/'docs/serve-config-v1.schema.json').read_text())
        value = json.loads((ROOT/'docs/evidence/serve-responses-business-20261109/business-01/config.json').read_text())
        validator = Draft202012Validator(schema)
        fields = schema['properties']['responses_background']['properties']
        # Exact dynamic shape from HandleConfig, no inference or listener.
        current = {key: ('ready' if key == 'state' else False if
            key in ('readiness_dependency', 'recovery_configured', 'recovery_scanned') else 0)
                   for key in fields}
        value['responses_background'] = current
        validator.validate(value)
        for key, invalid in [('state', 'healthy'), ('owned', -1),
                             ('recovery_scanned', 1), ('readiness_dependency', True)]:
            bad = copy.deepcopy(value)
            bad['responses_background'][key] = invalid
            self.assertFalse(validator.is_valid(bad))


class VaultCredentialSchema(unittest.TestCase):
    def test_public_resource_excludes_secrets_and_inputs_mark_them_write_only(self):
        auth = dict(type='static_bearer', mcp_server_url='https://records.example.test/mcp')
        row = dict(id='cred_1', vault_id='vault_1', object='vault.credential',
                   name='project', metadata={}, created_at=1, updated_at=1, auth=auth)
        self.assertFalse(errors(row, 'credential_resource'))
        self.assertTrue(errors(dict(row, auth=dict(auth, token='synthetic')), 'credential_resource'))
        self.assertFalse(errors(dict(name='project', auth=dict(auth, token='synthetic')), 'credential_create'))
        self.assertTrue(errors(dict(name='project', auth=auth), 'credential_create'))
        self.assertFalse(errors(dict(auth=dict(type='static_bearer', token='synthetic')), 'credential_update'))
        self.assertTrue(errors(dict(auth=dict(type='static_bearer', mcp_server_url='different')),
                               'credential_update'))
        secret_fields = {'access_token', 'refresh_token', 'client_secret', 'secret_value', 'token'}
        marked = set()
        for name, schema in SCHEMA['$defs'].items():
            if name.startswith('credential_'):
                for key, value in schema.get('properties', {}).items():
                    if key in secret_fields:
                        self.assertTrue(value.get('writeOnly'))
                        marked.add(key)
        self.assertEqual(marked, secret_fields)


class DeliveredRefusalSchema(unittest.TestCase):
    def test_existing_real_json_refusal_uses_unified_application_shape(self):
        path = ROOT/'docs/evidence/serve-refusal-business-20261112/chat-refusal.wire'
        value = json.loads(path.read_bytes())
        self.assertTrue(value['choices'][0]['message']['refusal'])
        self.assertFalse(errors(value, 'chat_completion'))
        broken = copy.deepcopy(SCHEMA['$defs'])
        del broken['chat_completion']['properties']['choices']['items'][
            'properties']['message']['properties']['refusal']
        invalid = copy.deepcopy(value)
        invalid['choices'][0]['message']['refusal'] = 17
        self.assertTrue(errors(invalid, 'chat_completion'))
        # SDK response objects allow unknown extensions: omitting the typed
        # field silently accepts an invalid refusal rather than rejecting it.
        self.assertTrue(Draft202012Validator(dict(app.ref('chat_completion'),
            **{'$defs': broken})).is_valid(invalid))

    def test_existing_real_sse_refusal_uses_unified_application_shape(self):
        path = ROOT/('docs/evidence/serve-refusal-managed-business-20261112/'
                     'version-business-01/execution/native-refusal.wire')
        values = [json.loads(line[6:]) for line in path.read_text().splitlines()
                  if line.startswith('data: ') and line != 'data: [DONE]']
        refusals = [v for v in values if any(c.get('delta', {}).get('refusal')
                                             for c in v['choices'])]
        self.assertTrue(refusals)
        for value in values:self.assertFalse(errors(value, 'chat_chunk'))
        broken = copy.deepcopy(SCHEMA['$defs'])
        del broken['chat_chunk']['properties']['choices']['items'][
            'properties']['delta']['properties']['refusal']
        invalid = copy.deepcopy(refusals[0])
        invalid['choices'][0]['delta']['refusal'] = 17
        self.assertTrue(errors(invalid, 'chat_chunk'))
        self.assertTrue(Draft202012Validator(dict(app.ref('chat_chunk'),
            **{'$defs': broken})).is_valid(invalid))


if __name__ == '__main__':
    unittest.main()
