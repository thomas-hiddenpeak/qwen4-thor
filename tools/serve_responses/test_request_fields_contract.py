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
