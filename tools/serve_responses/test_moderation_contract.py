"""Moderation wire contract against actual native C++ output and SSE."""
import os
from pathlib import Path
import unittest

from tools.serve_responses.test_contract import errors, wire_values


class ModerationContract(unittest.TestCase):
    def test_standard_moderation_model_and_independent_policies(self):
        for input_mode in ('score', 'block'):
            for output_mode in ('score', 'block'):
                self.assertEqual(errors(dict(input='question', moderation=dict(
                    model='actual-qwen', policy=dict(input=dict(mode=input_mode),
                    output=dict(mode=output_mode)))), 'response_create'), [])
        for invalid in ({}, dict(model=42), dict(model='actual-qwen',
                policy=dict(input=dict(mode='skip')))):
            self.assertTrue(errors(dict(input='question', moderation=invalid),
                                   'response_create'))

    def test_actual_cpp_filtered_json_and_original_sse_are_safe_and_accounted(self):
        values = [(path, kind, value) for path, kind, value in
                  wire_values(Path(os.environ['Q4T_TEST_OUTPUT_DIR']))
                  if path.name.startswith('moderation-block.')]
        self.assertGreaterEqual(len(values), 3)
        for path, kind, value in values:
            self.assertEqual(errors(value, kind), [], str(path))
        response = next(v for _, kind, v in values if kind == 'response')
        self.assertEqual(response['status'], 'incomplete')
        self.assertEqual(response['incomplete_details']['reason'], 'content_filter')
        self.assertEqual(response['output'], [])
        self.assertTrue(response['moderation']['output']['flagged'])
        self.assertEqual(response['usage']['total_tokens'], 93)
        self.assertEqual(response['q4t_moderation_usage']['total_tokens'], 140)
        events = [v for _, kind, v in values if kind == 'response_event']
        self.assertEqual([v['sequence_number'] for v in events], list(range(len(events))))
        self.assertFalse(any(v['type'].endswith('.delta') for v in events))
        self.assertEqual(events[-1]['type'], 'response.incomplete')
        self.assertEqual(events[-1]['response'], response)


if __name__ == '__main__':
    unittest.main()
