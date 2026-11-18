"""Negative controls for the real TTS comparison gate; no model loads."""
import copy
from pathlib import Path
import tempfile
import unittest

import numpy as np

from tools.native_tts import acceptance as gate


class AcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=gate.ROOT / '.q4t-work')
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def test_operand_shape_finite_and_drift_fail_closed(self):
        a, b = self.directory / 'a', self.directory / 'b'
        values = np.array([1., 2., 3., 4.], dtype=np.float32)

        def save(path, value):
            value.astype('<f4').tofile(path)

        save(a, values)
        save(b, values)
        threshold = gate.THRESHOLDS['dense']
        self.assertTrue(gate.compare_pair(a, b, 4, threshold)['passed'])
        save(b, -values)
        self.assertFalse(gate.compare_pair(a, b, 4, threshold)['passed'])
        for invalid in (values[:3], np.zeros(4, np.float32),
                        np.array([1, 2, np.nan, 4], np.float32),
                        np.array([1, 2, np.inf, 4], np.float32)):
            save(b, invalid)
            with self.subTest(invalid=invalid.tolist()), self.assertRaises(AssertionError):
                gate.compare_pair(a, b, 4, threshold)

    def test_teacher_forced_mismatch_and_invalid_codes(self):
        path = self.directory / 'codes'
        codes = np.arange(32, dtype='<i4').reshape(2, 16)
        codes.tofile(path)
        record = dict(expected_greedy_codes=codes.tolist(),
                      native_codes=codes.tolist(), teacher_forced_frames=2,
                      eos_qualified=False)
        self.assertTrue(gate.compare_codes(path, record)['passed'])
        record['expected_greedy_codes'][1][5] += 1
        self.assertFalse(gate.compare_codes(path, record)['passed'])
        for value in (-1, 2048, 2150):
            invalid = codes.copy()
            invalid[0, 0] = value
            invalid.tofile(path)
            with self.subTest(value=value), self.assertRaises(AssertionError):
                gate.compare_codes(path, record)

    def test_plan_and_operand_budget_cannot_expand(self):
        plan = dict(schema='q4t-native-tts-component-fp32-v1', limits=gate.LIMITS,
                    thresholds=gate.THRESHOLDS, cases=gate.CASES,
                    eos_qualified=False, audio_qualified=False)
        gate.validate_plan(plan)
        mutations = [('limits', 'gpu_model_loads', 2),
                     ('limits', 'seconds', 1201),
                     ('thresholds', 'identical_teacher_forced_greedy_codes', False)]
        for group, field, value in mutations:
            changed = copy.deepcopy(plan)
            changed[group][field] = value
            with self.subTest(field=field), self.assertRaises(AssertionError):
                gate.validate_plan(changed)
        for field in ('eos_qualified', 'audio_qualified'):
            with self.subTest(field=field), self.assertRaises(AssertionError):
                gate.validate_plan(dict(plan, **{field: True}))
        names = gate.operands(dict(text_tokens=16, prefill_tokens=12))
        self.assertEqual(len(names), 66)
        self.assertEqual(len({x[0] for x in names}), 66)
        with self.assertRaises(AssertionError):
            gate.operands(dict(text_tokens=513, prefill_tokens=12))


if __name__ == '__main__':
    unittest.main()
