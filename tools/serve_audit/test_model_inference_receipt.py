"""Reject defects shared by sync and streamed stock SDK results."""
import copy
import json
from pathlib import Path
import unittest

from .model_inference_receipt import verify


class InferenceReceipts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = (Path(__file__).resolve().parents[2] / 'docs/evidence' /
                  'serve-model-inference-sdk-20261128/cpu-fixture')
        cls.scenario = json.loads((folder / 'scenario.json').read_bytes())
        cls.actor = json.loads((folder / 'python-3.55.1.json').read_bytes())

    def test_fixture(self):
        self.assertEqual(len(verify(self.actor, self.scenario, self.actor['sdk'])), 7)

    def test_both_transports_can_agree_on_invalid_observations(self):
        for label, field, index, value in [
                ('negative offset', 'text_offset', -1, -1),
                ('outside text', 'text_offset', -1, len(self.actor['completion']['choices'][0]['text']) + 1),
                ('token probability', 'token_logprobs', -1, 0.5),
                ('alternative probability', 'top_logprobs', -1, {'604': 0.5}),
                ('first token', 'token_logprobs', 0, 0)]:
            with self.subTest(label=label):
                actor = copy.deepcopy(self.actor)
                logs = actor['completion']['choices'][0]['logprobs']
                logs[field][index] = value
                parts = [c['logprobs'] for frame in actor['chunks'] for c in frame['choices']
                         if c.get('logprobs') and c['logprobs'][field]]
                parts[index][field][index] = value
                self.assertEqual(logs[field], [v for part in parts for v in part[field]])
                with self.assertRaises(ValueError):
                    verify(actor, self.scenario, actor['sdk'])

    def test_model_owner_must_be_present_in_both_views(self):
        actor = copy.deepcopy(self.actor)
        actor['model']['owned_by'] = None
        next(m for m in actor['catalogue'] if m['id'] == actor['model']['id'])['owned_by'] = None
        with self.assertRaises(ValueError):
            verify(actor, self.scenario, actor['sdk'])
