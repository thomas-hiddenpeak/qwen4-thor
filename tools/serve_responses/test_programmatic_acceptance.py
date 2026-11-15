"""Failure-sensitive checks for the bounded programmatic business driver."""
import copy
import json
import os
from pathlib import Path
import unittest

from tools.serve_responses import programmatic_acceptance as driver
from tools.serve_responses.test_contract import errors


class ProgrammaticAcceptance(unittest.TestCase):
    def artifact(self, name):
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR']) / 'program-api-wire'
        return json.loads((root / (name + '.json')).read_text())

    def test_requests_match_sdk_and_frozen_limits(self):
        self.assertEqual(driver.LIMITS, dict(starts=1, chats=8, controls=2000, seconds=900))
        self.assertEqual(driver.Stage.max_output_tokens, 256)
        rows = driver.requests()
        for name, row in rows.items():
            self.assertEqual([(list(e.path), e.message[:120])
                              for e in errors(row, 'response_create')], [], name)
            self.assertLessEqual(row['max_output_tokens'], 256)
        for name in ['function', 'custom']:
            self.assertEqual(rows[name]['tool_choice'], dict(type='programmatic_tool_calling'))
            self.assertEqual(rows[name]['tools'][1]['tools'][0]['allowed_callers'],
                             ['programmatic'])
            self.assertFalse(rows[name]['parallel_tool_calls'])
        self.assertFalse(rows['function']['stream'])
        self.assertTrue(rows['custom']['stream'])
        self.assertEqual(rows['truncated']['max_output_tokens'], 1)

    def test_actual_cpp_program_and_resume_match_wire_schema(self):
        pending, completed = self.artifact('pending'), self.artifact('response')
        parent, _ = driver.program_calls(pending, name='read', namespace=None)
        driver.program_result(completed, parent, 42)
        for response in (pending, completed):
            self.assertEqual(errors(response, 'response'), [])
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR']) / 'program-api-wire'
        events = [json.loads(line[6:]) for line in
                  (root / 'response.wire').read_text().splitlines()
                  if line.startswith('data: ')]
        self.assertEqual([e['sequence_number'] for e in events], list(range(len(events))))
        self.assertEqual(events[-1]['type'], 'response.completed')
        for event in events:
            self.assertEqual(errors(event, 'response_event'), [], event['type'])
        done = [e['item'] for e in events if e['type'] == 'response.output_item.done']
        self.assertEqual(done[0], completed['output'][0])

    def test_child_oracle_rejects_identity_order_and_status_errors(self):
        original = self.artifact('pending')
        for index, key, value in [(0, 'fingerprint', ''), (0, 'status', 'completed'),
                                  (1, 'caller', dict(type='program', caller_id='wrong')),
                                  (1, 'name', 'unauthorized'),
                                  (1, 'namespace', 'other'), (1, 'status', 'incomplete')]:
            changed = copy.deepcopy(original)
            changed['output'][index][key] = value
            with self.assertRaises(RuntimeError):
                driver.program_calls(changed, name='read', namespace=None)
        changed = copy.deepcopy(original)
        changed['output'].reverse()
        with self.assertRaises(RuntimeError):
            driver.program_calls(changed, name='read', namespace=None)
        # Synthetic custom public-shape negative control, not model evidence.
        custom = copy.deepcopy(original)
        call = custom['output'][1]
        call.update(type='custom_tool_call', name='raw', namespace='project', input='read')
        call.pop('arguments')
        call.pop('status')
        driver.program_calls(custom, custom=True)
        call['status'] = 'completed'
        with self.assertRaises(RuntimeError):
            driver.program_calls(custom, custom=True)

    def test_completion_and_get_projection_are_failure_sensitive(self):
        pending, original = self.artifact('pending'), self.artifact('response')
        parent = pending['output'][0]
        for key, value in [('call_id', 'wrong'), ('status', 'incomplete'), ('result', '41')]:
            changed = copy.deepcopy(original)
            changed['output'][0][key] = value
            with self.assertRaises(RuntimeError):
                driver.program_result(changed, parent, 42)
        changed = copy.deepcopy(original)
        changed['output'].append(pending['output'][1])
        with self.assertRaises(RuntimeError):
            driver.program_result(changed, parent, 42)
        projected = driver.stored_projection(original)
        self.assertEqual(projected['output'][0], original['output'][0])
        self.assertNotIn('logprobs', projected['output'][1]['content'][0])
        changed = copy.deepcopy(projected)
        changed['output'][0]['result'] = '41'
        self.assertNotEqual(changed, driver.stored_projection(original))


if __name__ == '__main__':
    unittest.main()
