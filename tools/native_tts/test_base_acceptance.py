"""Negative controls for registered-condition qualification identity."""
import copy
import os
from pathlib import Path
import struct
import tempfile
import unittest

from . import base_acceptance as acceptance


class BaseAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_TMP'])
        self.addCleanup(self.temp.cleanup)
        self.plan = dict(schema='q4t-native-base-tts-component-fp32-v1',
            limits=copy.deepcopy(acceptance.LIMITS),
            thresholds=copy.deepcopy(acceptance.THRESHOLDS),
            cases=copy.deepcopy(acceptance.CASES),
            eos_qualified=False, audio_qualified=False)
        for i, case in enumerate(self.plan['cases']):
            path = Path(self.temp.name) / f'{i}.f32'
            path.write_bytes(struct.pack('<2048f', *([i + 1.0] * 2048)))
            case.update(speaker_embedding=str(path),
                        speaker_embedding_sha256=acceptance.digest(path))

    def test_two_owned_distinct_conditions_are_accepted(self):
        acceptance.validate_plan(self.plan)

    def test_changed_truncated_zero_or_nonfinite_condition_is_rejected(self):
        row = self.plan['cases'][0]
        path = Path(row['speaker_embedding'])
        original = path.read_bytes()
        for payload, resign in ((original[:-4], True),
                                (bytes(len(original)), True),
                                (struct.pack('<f', float('nan')) + original[4:], True),
                                (struct.pack('<f', 4.0) + original[4:], False)):
            path.write_bytes(payload)
            if resign:
                row['speaker_embedding_sha256'] = acceptance.digest(path)
            with self.assertRaises(AssertionError):
                acceptance.validate_plan(self.plan)
            path.write_bytes(original)
            row['speaker_embedding_sha256'] = acceptance.digest(path)

    def test_builtin_substitution_duplicate_or_missing_condition_is_rejected(self):
        mutations = [lambda p: p['cases'][0].update(voice='ryan'),
            lambda p: p['cases'][1].update({k: p['cases'][0][k] for k in
                ('speaker_embedding', 'speaker_embedding_sha256')}),
            lambda p: p['cases'][0].update(speaker_embedding='/nonexistent/voice')]
        for change in mutations:
            plan = copy.deepcopy(self.plan)
            change(plan)
            with self.assertRaises(AssertionError):
                acceptance.validate_plan(plan)

    def test_relaxed_gate_budget_or_audio_claim_is_rejected(self):
        mutations = [lambda p: p['thresholds']['dense'].update(relative_l2=1),
            lambda p: p['limits'].update(gpu_model_loads=2),
            lambda p: p.update(audio_qualified=True)]
        for change in mutations:
            plan = copy.deepcopy(self.plan)
            change(plan)
            with self.assertRaises(AssertionError):
                acceptance.validate_plan(plan)


if __name__ == '__main__':
    unittest.main()
