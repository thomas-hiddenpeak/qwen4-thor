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
                         fields | {'request_id', 'cancel_token', 'request_timeout_ms', 'seed'})
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
        self.assertEqual({method.upper()+' '+path for path, methods in spec['paths'].items()
            for method in methods}, set(json.loads((ROOT/'tools/serve_audit/inventory.json').read_text())['routes']))
        cancel = spec['paths']['/v1/responses/{response_id}/cancel']['post']
        self.assertIn('200', cancel['responses'])
        self.assertNotIn('requestBody', cancel)
        self.assertEqual([p['name'] for p in spec['paths']['/v1/responses/{response_id}']['get']['parameters']],
            ['response_id', 'include[]', 'include_obfuscation', 'starting_after', 'stream'])
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
