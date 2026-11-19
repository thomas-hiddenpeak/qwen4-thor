"""Acceptance-gate negatives use synthetic files; no model or CUDA execution."""
import copy
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest

from tools.native_tts import encoder_acceptance as gate


class EncoderAcceptance(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',
                                   '.q4t-work/tts-encoder-gate')).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def plan(self):
        return dict(schema='q4t-native-tts-encoder-fp32-v1',
            limits=copy.deepcopy(gate.LIMITS),
            thresholds=copy.deepcopy(gate.THRESHOLDS),
            cases=[dict(samples=n, pcm=str(self.root / str(i)), sha256='1' * 64)
                   for i, n in enumerate(gate.SAMPLES)],
            execution_dtype='F32', tf32=False, icl_qualified=False,
            speech_quality_qualified=False,
            attention='pinned-mimi-4.57.6-eager-full-causal')

    def codes(self, name, values):
        p = self.root / name
        p.write_bytes(struct.pack('<' + 'i' * len(values), *values))
        return p

    def test_plan_rejects_scope_precision_and_attention_substitution(self):
        gate.validate_plan(self.plan())
        for key, value in [('execution_dtype', 'BF16'), ('tf32', True),
                           ('icl_qualified', True), ('speech_quality_qualified', True),
                           ('attention', 'sliding-window-250')]:
            plan = self.plan()
            plan[key] = value
            with self.assertRaises(AssertionError):
                gate.validate_plan(plan)

    def test_plan_rejects_larger_budget_and_missing_long_case(self):
        plan = self.plan()
        plan['limits']['gpu_model_loads'] += 1
        with self.assertRaises(AssertionError):
            gate.validate_plan(plan)
        plan = self.plan()
        plan['cases'][-1]['samples'] = 24000
        with self.assertRaises(AssertionError):
            gate.validate_plan(plan)

    def test_operands_keep_both_tails_and_all_quantizers(self):
        short = dict(gate.operands(17))
        self.assertEqual(len(short), 74)
        self.assertEqual(short['encoder-downsample-0-1.f32'], 5 * 128)
        self.assertEqual(short['encoder-transformer-7-1.f32'], 512)
        self.assertEqual(short['encoder-rvq-residual-15-1.f32'], 256)
        long = dict(gate.operands(278419))
        self.assertEqual(long['encoder-transformer-7-1.f32'], 8 * 512)
        for samples in (0, 1440001):
            with self.assertRaises(AssertionError):
                gate.operands(samples)

    def test_complete_codes_compare_and_detect_single_difference(self):
        a = self.codes('a', [17] * 32)
        b = self.codes('b', [17] * 32)
        self.assertTrue(gate.compare_codes(a, b, 2)['passed'])
        b = self.codes('b', [17] * 31 + [18])
        result = gate.compare_codes(a, b, 2)
        self.assertFalse(result['passed'])
        self.assertEqual(result['differing'], 1)

    def test_partial_code_file_never_counts_as_complete(self):
        a = self.codes('a', [17] * 32)
        b = self.codes('b', [17] * 16)
        with self.assertRaises(AssertionError):
            gate.compare_codes(a, b, 2)

    def test_control_or_negative_codes_rejected_on_both_sides(self):
        for bad in (-1, 2048, 2150):
            a = self.codes('a', [17] * 16)
            b = self.codes('b', [17] * 15 + [bad])
            for left, right in ((a, b), (b, a)):
                with self.assertRaises(AssertionError):
                    gate.compare_codes(left, right, 1)

    def pair(self):
        directories = [self.root / n for n in ('native', 'reference')]
        for p in directories:
            p.mkdir()
            (p / 'result.json').write_text(json.dumps(dict(samples=17, frames=1,
                transformer_rows=1, mask=dict(rows=1, final_row_sees_first=True,
                    future_masked=True, sliding_window_applied=False))))
            (p / 'codes.i32').write_bytes(struct.pack('<16i', *([17] * 16)))
            for name, count in gate.operands(17):
                (p / name).write_bytes(struct.pack('<' + 'f' * count, *([1.] * count)))
        return directories

    def test_complete_stage_set_required(self):
        left, right = self.pair()
        checks = gate.compare_case(left, right, 17)
        self.assertEqual(len(checks), 75)
        self.assertTrue(all(c['passed'] for c in checks))
        (right / 'encoder-rvq-residual-15-1.f32').unlink()
        with self.assertRaises(AssertionError):
            gate.compare_case(left, right, 17)

    def test_incompatible_reference_mask_cannot_certify_native(self):
        left, right = self.pair()
        record = json.loads((right / 'result.json').read_text())
        record['mask']['final_row_sees_first'] = False
        (right / 'result.json').write_text(json.dumps(record))
        with self.assertRaises(AssertionError):
            gate.compare_case(left, right, 17)


if __name__ == '__main__':
    unittest.main()
