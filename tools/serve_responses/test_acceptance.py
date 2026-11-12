"""Direct failure-sensitive oracles for the foreground business short check."""
import copy
import unittest

from tools.serve_responses.acceptance import (
    LIMITS, requests, check_usage, check_probabilities)


class Driver(unittest.TestCase):
    def test_frozen_work_stays_within_business_budget(self):
        rows = requests()
        self.assertEqual(len(rows), 7)
        self.assertEqual(LIMITS, dict(starts=2, chats=8, controls=2000, seconds=900))
        self.assertEqual(rows['plain']['store'], False)
        self.assertEqual(rows['truncated']['max_output_tokens'], 1)
        self.assertEqual(rows['follow']['tool_choice'], 'none')
        self.assertEqual(rows['tool']['tools'][0]['parameters']['properties']['path']['const'],
                         'docs/MODEL.md')

    def test_usage_must_conserve_tokens_and_include_real_integer_details(self):
        value = dict(usage=dict(input_tokens=5, output_tokens=3, total_tokens=8,
            input_tokens_details=dict(cached_tokens=2, cache_write_tokens=0),
            output_tokens_details=dict(reasoning_tokens=1)))
        check_usage(value)
        for field in ('input_tokens', 'output_tokens', 'total_tokens'):
            bad = copy.deepcopy(value)
            bad['usage'][field] += 1
            with self.assertRaises(RuntimeError): check_usage(bad)
        for field, name, invalid in [('input_tokens_details', 'cached_tokens', 6),
                                    ('input_tokens_details', 'cache_write_tokens', 6),
                                    ('input_tokens_details', 'cache_write_tokens', None),
                                    ('output_tokens_details', 'reasoning_tokens', 4),
                                    ('output_tokens_details', 'reasoning_tokens', True)]:
            bad = copy.deepcopy(value); bad['usage'][field][name] = invalid
            with self.assertRaises(RuntimeError): check_usage(bad)

    def test_probability_oracle_requires_finite_scores_and_original_bytes(self):
        row = dict(token='�', bytes=[226], logprob=-0.5)
        value = dict(output=[dict(type='message', content=[dict(type='output_text',
            logprobs=[dict(row, top_logprobs=[row, row])])])])
        self.assertEqual(check_probabilities(value), 1)
        for field, invalid in [('bytes', [256]), ('bytes', None),
                               ('logprob', float('-inf')), ('logprob', 0.1)]:
            bad = copy.deepcopy(value)
            bad['output'][0]['content'][0]['logprobs'][0][field] = invalid
            with self.assertRaises(RuntimeError): check_probabilities(bad)


if __name__ == '__main__':
    unittest.main()
