"""Frozen business driver assertions, no model/listeners/GPU activity."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_sampling.acceptance import LIMITS, requests, require_probs, require_choices
from tools.serve_protocol.check_contract import load_validators


class Acceptance(unittest.TestCase):
    def test_plan_shapes_and_bounds(self):
        self.assertEqual(LIMITS, dict(starts=2, chats=12, controls=2000, seconds=900))
        bodies = requests(22)
        self.assertEqual(len(bodies)+1, 9)  # Tool result is based on actual execution.
        schema = load_validators()['chat']
        for body in bodies.values():
            schema.validate(body)
        self.assertNotIn('temperature', bodies['default'])
        self.assertEqual(bodies['sampled_json']['n'], 2)
        self.assertEqual(bodies['bias']['logit_bias'], {'22':100})
        self.assertEqual(bodies['greedy']['temperature'], 0)

    def test_probabilities_and_real_choices_are_required(self):
        record = dict(token='42', logprob=-0.1, bytes=[52,50], top_logprobs=[])
        row = dict(index=0, message=dict(role='assistant', content='42'),
                   finish_reason='stop', logprobs=dict(content=[record], refusal=None))
        require_probs(row)
        require_choices(SimpleNamespace(choices=[row, dict(row, index=1)]))
        for mutate in (lambda r:r.update(logprobs=None),
                       lambda r:r['logprobs']['content'][0].update(logprob=1),
                       lambda r:r['logprobs']['content'][0].update(bytes=[52,51])):
            bad = copy.deepcopy(row); mutate(bad)
            with self.assertRaises(RuntimeError):
                require_probs(bad)
        with self.assertRaises(RuntimeError):
            require_choices(SimpleNamespace(choices=[row, row]))

    def test_requests_reach_production_plan(self):
        probe = Path(os.environ['Q4T_SAMPLING_PROBE'])
        bodies = list(requests(22).values())
        bad = dict(bodies[0], temperature=3)
        run = subprocess.run([str(probe)], input=''.join(json.dumps(dict(raw=json.dumps(b)))+'\n'
            for b in [*bodies,bad]), text=True,capture_output=True,timeout=15)
        self.assertEqual(run.returncode, 0);self.assertEqual(run.stderr,'')
        values = list(map(json.loads,run.stdout.splitlines()))
        self.assertEqual([v['accepted'] for v in values], [True]*len(bodies)+[False])
        self.assertEqual(values[-1]['response']['error']['param'],'temperature')


if __name__ == '__main__':
    unittest.main()
