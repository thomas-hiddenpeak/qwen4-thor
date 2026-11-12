"""Direct pro schema oracles for actual C++ JSON and original SSE."""
import os
from pathlib import Path
import unittest

from tools.serve_responses.test_contract import errors, wire_values


class ReasoningContract(unittest.TestCase):
    def test_mode_and_effort_are_independent_standard_fields(self):
        for mode in ('standard', 'pro'):
            for effort in ('none', 'low', 'medium', 'xhigh'):
                self.assertEqual(errors(dict(input='17+25?', reasoning=dict(
                    mode=mode, effort=effort)), 'response_create'), [])
        for invalid in (False, 1, [], {}):
            self.assertTrue(errors(dict(input='x', reasoning=dict(mode=invalid)),
                                   'response_create'))

    def test_actual_cpp_output_and_events_keep_pro_usage_and_reasoning(self):
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        values = [(path, kind, value) for path, kind, value in wire_values(root)
                  if path.name.startswith('pro.')]
        self.assertGreater(len(values), 10)
        for path, kind, value in values:
            self.assertEqual(errors(value, kind), [], str(path))
        response = next(v for _, kind, v in values if kind == 'response')
        self.assertEqual(response['reasoning']['mode'], 'pro')
        self.assertEqual(response['reasoning']['effort'], 'none')
        self.assertEqual(response['usage']['output_tokens_details']['reasoning_tokens'], 9)
        events = [v for _, kind, v in values if kind == 'response_event']
        self.assertEqual([v['sequence_number'] for v in events], list(range(len(events))))
        self.assertEqual(events[-1]['type'], 'response.completed')
        self.assertEqual(events[-1]['response'], response)
        self.assertTrue(any(v['type'] == 'response.reasoning_text.delta' for v in events))


if __name__ == '__main__':
    unittest.main()
