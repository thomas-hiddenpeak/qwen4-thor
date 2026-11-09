"""Failure-sensitive acceptance oracles without loading a model."""
import copy
import unittest

from tools.serve_responses.context_acceptance import (
    LIMITS, check_error, check_history, requests)


class ContextAcceptance(unittest.TestCase):
    def test_work_is_bounded_and_expected_failures_cannot_be_accepted(self):
        rows = requests()
        self.assertEqual(len(rows), 7)
        self.assertEqual(LIMITS, dict(starts=2, chats=8, controls=2000, seconds=900))
        self.assertEqual(rows['disabled']['truncation'], 'disabled')
        self.assertEqual(len(rows['oversized']['input']), 1)
        self.assertTrue(rows['background']['background'])
        self.assertEqual(rows['sse']['input'], rows['json']['input'])
        good = dict(ok=False, error=dict(status=400, code='context_length_exceeded'))
        check_error(good)
        for value in (dict(good, ok=True), dict(ok=False, error=dict(status=503,
                code='response_executor_unavailable'))):
            with self.assertRaises(RuntimeError):
                check_error(value)

    def test_history_oracle_rejects_dropped_edited_or_reordered_items(self):
        expected = requests()['json']['input']
        good = dict(has_more=False, data=[dict(v, id=f'item_{i}')
                    for i, v in enumerate(expected)])
        check_history(good, expected)
        changes = [dict(good, has_more=True), dict(good, data=good['data'][1:]),
                   dict(good, data=list(reversed(good['data'])))]
        changed = copy.deepcopy(good)
        changed['data'][0]['content'] = 'truncated'
        changes.append(changed)
        for value in changes:
            with self.assertRaises(RuntimeError):
                check_history(value, expected)


if __name__ == '__main__':
    unittest.main()
