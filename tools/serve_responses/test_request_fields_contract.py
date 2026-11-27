"""Check frozen SDK defaults against the public schema, independently of C++."""
import hashlib
import json
import unittest

from tools.serve_responses.test_contract import ROOT, SCHEMA, errors, resolve


class RequestFieldsContract(unittest.TestCase):
    def test_complete_sdk_null_and_tier_vectors_match_schema(self):
        fixture = json.loads((ROOT / 'tests/fixtures/responses_fields/sdk_vectors.json').read_text())
        graph = (ROOT / 'docs/evidence/serve-input-tokens-cpu-20261110/application-types.json').read_bytes()
        self.assertEqual(hashlib.sha256(graph).hexdigest(), fixture['graph_sha256'])
        self.assertEqual(fixture['graph_sha256'], SCHEMA['x-q4t']['type_graph_sha256'])
        self.assertEqual(fixture['sources_sha256'], json.loads(graph)['sources_sha256'])
        for source, digest in fixture['sources_sha256'].items():
            self.assertEqual(SCHEMA['x-q4t']['sources_sha256'][source], digest)
        vectors = fixture['vectors']
        self.assertEqual(len(vectors), 44)
        properties = resolve(SCHEMA['$defs']['response_create'])['properties']
        later_fields = {'image', 'personality', 'seed', 'request_id', 'cancel_token', 'request_timeout_ms'}
        self.assertEqual({r['name'][:-5] for r in vectors
                          if r['name'].endswith('-null') and '.' not in r['name']},
                         set(properties) - later_fields)
        for row in vectors:
            with self.subTest(row=row['name']):
                expected = row.get('wire_accepted', row['accepted'])
                self.assertEqual(not errors(row['request'], 'response_create'), expected)

    def test_response_create_defaults_on_stable_and_agent_requests(self):
        expected = dict(store=True, background=False, stream=False,
                        parallel_tool_calls=True, temperature=1, top_p=1,
                        conversation=None, service_tier='auto', truncation='disabled')
        for name in ('response_create', 'agent_history_response_create'):
            properties = resolve(SCHEMA['$defs'][name])['properties']
            with self.subTest(name=name):
                actual = {key: properties[key]['default'] for key in expected}
                self.assertEqual(json.dumps(actual), json.dumps(expected))
                self.assertEqual(errors(dict(input='Hello', **actual), name), [])

    def test_saved_prompt_does_not_supply_create_defaults(self):
        definition = resolve(SCHEMA['$defs']['prompt_definition'])
        properties = definition['properties']['request']['properties']
        for key in ('temperature', 'top_p', 'parallel_tool_calls', 'truncation'):
            self.assertNotIn('default', properties[key])

    def test_default_stop_and_bias_on_all_completion_inputs(self):
        bundle = json.loads((ROOT / 'docs/serve-openapi-bundle-v1.json').read_bytes())
        for family in ('chat', 'vision', 'completion'):
            name = f'serve-{family}-request-v1.schema.json'
            standalone = json.loads((ROOT / 'docs' / name).read_bytes())
            for document in (standalone, bundle['components']['schemas'][name]):
                with self.subTest(schema=name):
                    for field in ('stop', 'logit_bias'):
                        self.assertIsNone(document['properties'][field]['default'])

    def test_nullable_user_on_stable_and_agent_wire(self):
        for kind in ('response_create', 'agent_history_response_create'):
            with self.subTest(kind=kind):
                self.assertTrue(resolve(SCHEMA['$defs'][kind])['properties']['user']['deprecated'])
                for user in (None, 'client'):
                    self.assertEqual(errors(dict(input='hi', user=user), kind), [])
                for user in (42, False, [], {}):
                    self.assertTrue(errors(dict(input='hi', user=user), kind))

    def test_count_compact_and_create_keep_distinct_null_contracts(self):
        count = dict(input=None, model=None, text=None, tools=None, tool_choice=None)
        self.assertEqual(errors(count, 'input_tokens_request'), [])
        self.assertTrue(errors(count, 'response_create'))
        compact = dict(model=None, input=None, prompt_cache_options=None, service_tier='fast')
        self.assertEqual(errors(compact, 'response_compact'), [])
        self.assertTrue(errors(compact, 'response_create'))
        self.assertEqual(errors(dict(instructions='Answer briefly.'), 'response_create'), [])


if __name__ == '__main__':
    unittest.main()
