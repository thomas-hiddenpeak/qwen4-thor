"""Offline negative checks for the fixed R02 acceptance verdict."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_grammar_reap.acceptance import LIMITS, fault_terminal, requests


def wire(values, done=True):
    return b''.join(b'data: '+json.dumps(value).encode()+b'\n\n'
                    for value in values) + (b'data: [DONE]\n\n' if done else b'')


class Acceptance(unittest.TestCase):
    def test_complete_error_is_distinct_from_success(self):
        error = dict(error=dict(code='constraint_compile_timeout'))
        self.assertTrue(fault_terminal(200, wire([error]))['complete_error'])
        success = dict(choices=[dict(finish_reason='stop')], usage=dict(total_tokens=1))
        for values in ([success, error], [error, error], [error, success], [success]):
            with self.assertRaises(RuntimeError):
                fault_terminal(200, wire(values))

    def test_missing_terminal_and_wrong_failure_cannot_pass(self):
        error = dict(error=dict(code='constraint_compile_timeout'))
        for status, raw in ((503, wire([error])), (200, wire([error], False)),
                            (200, wire([dict(error=dict(code='gpu_unhealthy'))]))):
            with self.assertRaises(RuntimeError):
                fault_terminal(status, raw)

    def test_frozen_requests_respect_stage_budget_and_strict_contract(self):
        self.assertEqual(LIMITS, dict(starts=1, chats=6, controls=2000, seconds=600))
        bodies = requests()
        self.assertNotIn('tools', bodies['plain'])
        self.assertNotIn('response_format', bodies['plain'])
        self.assertTrue(bodies['tool']['tools'][0]['function']['strict'])
        self.assertEqual(bodies['tool']['tool_choice'], 'required')
        self.assertTrue(bodies['fault']['stream'])
        self.assertEqual(bodies['fault']['response_format']['json_schema']['schema']
                         ['properties']['data']['maxItems'], 64)


if __name__ == '__main__':
    unittest.main()
