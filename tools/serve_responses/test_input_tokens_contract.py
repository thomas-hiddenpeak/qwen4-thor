"""Only the new count endpoint and its actual CPU renderer output."""
import json
import os
from pathlib import Path
import unittest

from tools.serve_responses.test_contract import errors, ROOT


class InputTokensContract(unittest.TestCase):
    def test_request_and_actual_count_output(self):
        for value in ({}, dict(model=None, input=None),
                      dict(input=[], personality='friendly'),
                      dict(input='hello', personality='pragmatic')):
            self.assertEqual(errors(value, 'input_tokens_request'), [])
        for value in (dict(stream=True), dict(personality=None),
                      dict(personality='unknown'), dict(truncation=None),
                      dict(previous_response_id='resp_x', conversation='conv_y')):
            self.assertTrue(errors(value, 'input_tokens_request'))
        output = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])/'input-tokens.json'
        value = json.loads(output.read_text())
        self.assertEqual(errors(value, 'input_tokens_response'), [])
        self.assertEqual(value, dict(object='response.input_tokens', input_tokens=42))
        self.assertTrue(errors(dict(value, input_tokens=1.5), 'input_tokens_response'))

    def test_native_route_optional_body_and_generation_personality(self):
        api = json.loads((ROOT/'docs/serve-openapi-v1.json').read_text())
        route = api['paths']['/v1/responses/input_tokens']
        self.assertEqual(set(route), {'post'})
        self.assertFalse(route['post']['requestBody']['required'])
        self.assertEqual(errors(dict(input='hi', personality='friendly'),
                                'response_create'), [])
        self.assertTrue(errors(dict(input='hi', personality=None),
                               'response_create'))


if __name__ == '__main__':
    unittest.main()
