"""Failure-sensitive checks for the bounded A06 business driver."""
import copy
import json
import os
import subprocess
import unittest

from tools.serve_completions.acceptance import LIMITS, requests, best_of_expected


class Driver(unittest.TestCase):
    def test_frozen_inputs_pass_production_semantics_before_gpu(self):
        rows = requests()
        cases = [dict(kind='chat' if name == 'chat' else 'completion',
                      token_vocab=300000, raw=json.dumps(value))
                 for name, value in rows.items()]
        run = subprocess.run([os.environ['Q4T_COMPLETION_PROBE']],
            input=''.join(json.dumps(row)+'\n' for row in cases),
            capture_output=True, text=True, timeout=20, check=True)
        answers = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual(len(answers), len(rows))
        self.assertTrue(all(row['accepted'] for row in answers), answers)
        self.assertEqual(LIMITS, dict(starts=2, chats=8, controls=2000, seconds=900))

    def test_best_of_oracle_uses_each_prompt_group_and_actual_mean(self):
        choices = []
        for i, score in enumerate([-2, -1, -1, -0.2, -0.7, -0.5]):
            choices.append(dict(index=i, text=str(i), finish_reason='length',
                logprobs=dict(token_logprobs=[score]*4)))
        actual = best_of_expected(dict(choices=choices))
        self.assertEqual([row['candidate']['text'] for row in actual], ['1', '3'])
        self.assertEqual([row['index'] for row in actual], [0, 1])
        for mutation in ('eos', 'missing', 'index', 'positive'):
            altered = copy.deepcopy(choices)
            if mutation == 'eos': altered[0]['finish_reason'] = 'stop'
            if mutation == 'missing': altered[0]['logprobs']['token_logprobs'].pop()
            if mutation == 'index': altered[0]['index'] = 5
            if mutation == 'positive': altered[0]['logprobs']['token_logprobs'][0] = 0.1
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                best_of_expected(dict(choices=altered))


if __name__ == '__main__':
    unittest.main()
