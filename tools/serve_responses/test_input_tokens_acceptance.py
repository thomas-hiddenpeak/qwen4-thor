"""Bounded count driver and failure oracles, CPU only."""
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest

from tools.serve_responses import input_tokens_acceptance as driver
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read


class InputTokensAcceptance(unittest.TestCase):
    def test_frozen_requests_and_count_contract(self):
        self.assertEqual(driver.LIMITS, dict(starts=1, chats=4, controls=2000, seconds=900))
        self.assertEqual(driver.MAX_COUNTS, 16)
        rows = driver.requests()
        self.assertEqual(set(rows), {'initial', 'tool', 'follow', 'chat'})
        for name, row in rows.items():
            if name == 'chat':
                continue
            self.assertEqual(errors(row, 'response_create'), [], name)
            count = driver.count_request(row)
            self.assertEqual(errors(count, 'input_tokens_request'), [], name)
            self.assertNotIn('store', count)
            self.assertNotIn('max_output_tokens', count)
            self.assertNotIn('stream', count)
            for key in count:
                self.assertEqual(count[key], row[key])
        self.assertEqual(rows['initial']['personality'], 'friendly')
        self.assertEqual(rows['follow']['personality'], 'pragmatic')

    def test_exact_usage_oracle_rejects_off_by_one_and_fabricated_counts(self):
        # Already accepted real response, reused only as a shape/usage fixture.
        evidence = driver.ROOT/'docs/evidence/serve-access-program-business-20261110'
        paths = list(evidence.rglob('implicit.sdk.json'))
        self.assertEqual(len(paths), 1)
        response = read(paths[0])['result']
        count = response['usage']['input_tokens']
        driver.check_count_usage(count, response)
        with self.assertRaises(RuntimeError):
            driver.check_count_usage(count+1, response)
        value = dict(object='response.input_tokens', input_tokens=count)
        self.assertEqual(driver.count_value(value), count)
        for wrong in (dict(value, input_tokens=True), dict(value, input_tokens=0),
                      dict(value, input_tokens=1.5), dict(value, generated=True)):
            with self.assertRaises(RuntimeError):
                driver.count_value(wrong)

    def test_count_budget_and_no_argument_sdk_call_do_not_create_response(self):
        calls = []
        def sdk(*args, **kwargs):
            calls.append((args, kwargs))
            return dict(object='response.input_tokens', input_tokens=7)
        stage = SimpleNamespace(input_counts=15, sdk=sdk)
        self.assertEqual(driver.Stage.count(stage, 'empty', omit_request=True), 7)
        self.assertEqual(calls[0][0], ('empty', 'input_tokens', None))
        self.assertTrue(calls[0][1]['omit_request'])
        with self.assertRaises(RuntimeError):
            driver.Stage.count(stage, 'over-budget')
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
