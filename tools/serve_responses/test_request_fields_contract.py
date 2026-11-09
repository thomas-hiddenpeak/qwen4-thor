"""Check frozen SDK defaults against the public schema, independently of C++."""
import json
import unittest

from tools.serve_responses.test_contract import ROOT, SCHEMA, errors, resolve


class RequestFieldsContract(unittest.TestCase):
    def test_complete_sdk_null_and_tier_vectors_match_schema(self):
        fixture = json.loads((ROOT / 'tests/fixtures/responses_fields/sdk_vectors.json').read_text())
        self.assertEqual(fixture['sources_sha256'], SCHEMA['x-q4t']['sources_sha256'])
        vectors = fixture['vectors']
        self.assertEqual(len(vectors), 44)
        properties = resolve(SCHEMA['$defs']['response_create'])['properties']
        native = {'personality', 'seed', 'request_id', 'cancel_token', 'request_timeout_ms'}
        self.assertEqual({r['name'][:-5] for r in vectors
                          if r['name'].endswith('-null') and '.' not in r['name']},
                         set(properties) - native)
        for row in vectors:
            with self.subTest(row=row['name']):
                self.assertEqual(not errors(row['request'], 'response_create'), row['accepted'])

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
