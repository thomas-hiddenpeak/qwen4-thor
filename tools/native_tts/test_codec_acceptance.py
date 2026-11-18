"""Codec waveform/comparison negative controls; no model or GPU execution."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from tools.native_tts import codec_acceptance as gate


class CodecAcceptanceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=gate.ROOT / '.q4t-work')
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)

    def test_waveform_drift_finite_and_sample_count(self):
        a, b = self.directory / 'a', self.directory / 'b'
        wave = np.array([0.1, -0.2, 0.3, -0.4], dtype='<f4')
        wave.tofile(a)
        wave.tofile(b)
        self.assertTrue(gate.compare_pair(a, b, 4, gate.THRESHOLDS)['passed'])
        (-wave).tofile(b)
        self.assertFalse(gate.compare_pair(a, b, 4, gate.THRESHOLDS)['passed'])
        for invalid in (wave[:3], np.zeros(4), [0.1, np.nan, 0.3, -0.4],
                        [0.1, np.inf, 0.3, -0.4]):
            np.array(invalid, dtype='<f4').tofile(b)
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                gate.compare_pair(a, b, 4, gate.THRESHOLDS)

    def test_fixed_budget_and_cases(self):
        plan = dict(schema='q4t-native-tts-codec-fp32-v1', limits=gate.LIMITS,
                    thresholds=gate.THRESHOLDS, cases=gate.CASES,
                    eos_qualified=False, speech_quality_qualified=False,
                    execution_dtype='F32', tf32=False)
        gate.validate_plan(plan)
        for group, key, value in (('limits', 'gpu_model_loads', 2),
                                  ('limits', 'seconds', 1201),
                                  ('thresholds', 'relative_l2', 0.01)):
            changed = copy.deepcopy(plan)
            changed[group][key] = value
            with self.subTest(key=key), self.assertRaises(AssertionError):
                gate.validate_plan(changed)
        for key in ('eos_qualified', 'speech_quality_qualified', 'tf32'):
            with self.subTest(key=key), self.assertRaises(AssertionError):
                gate.validate_plan(dict(plan, **{key: True}))
        changed = copy.deepcopy(plan)
        changed['cases'][1]['codes'][0][0] += 1
        with self.assertRaises(AssertionError):
            gate.validate_plan(changed)
        self.assertEqual(sum(len(gate.operands(c[1])) for cs in gate.CHUNKS for c in cs), 90)

    def test_chunk_and_trace_coverage_fail_closed(self):
        native, reference = self.directory / 'n', self.directory / 'r'
        record = dict(samples=3840, chunks=[dict(input_frames=2, input_begin=0,
            discard_samples=0, output_samples=3840, workspace_bytes=1000)])
        for directory in (native, reference):
            (directory / 'chunk-0').mkdir(parents=True)
            (directory / 'result.json').write_text(json.dumps(record))
            np.full(3840, 0.1, dtype='<f4').tofile(directory / 'pcm.f32')
            for name, elements in gate.operands(2):
                np.full(elements, 0.1, dtype='<f4').tofile(directory / 'chunk-0' / name)
        self.assertTrue(all(c['passed'] for c in gate.compare_case(native, reference, 0)))
        extra = native / 'chunk-0/extra.f32'
        extra.write_bytes(b'')
        with self.assertRaises(AssertionError):
            gate.compare_case(native, reference, 0)
        extra.unlink()
        record['chunks'][0]['discard_samples'] = 1920
        (native / 'result.json').write_text(json.dumps(record))
        with self.assertRaises(AssertionError):
            gate.compare_case(native, reference, 0)
        record['chunks'] = []
        (native / 'result.json').write_text(json.dumps(record))
        with self.assertRaises(AssertionError):
            gate.compare_case(native, reference, 0)


if __name__ == '__main__':
    unittest.main()
