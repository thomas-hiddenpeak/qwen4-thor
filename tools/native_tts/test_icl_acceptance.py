"""Independent input/qualification negative controls; no model or CUDA import."""
import copy
import os
from pathlib import Path
import struct
import tempfile
import unittest

from . import icl_acceptance as acceptance
from .icl_reference import apply_icl_inputs


class IclAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_TMP'])
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        vector, codes = root / 'speaker.f32', root / 'codes.i32'
        vector.write_bytes(struct.pack('<2048f', *([1.0] * 2048)))
        codes.write_bytes(struct.pack('<480i', *range(480)))
        self.plan = dict(schema='q4t-native-icl-tts-component-fp32-v1',
            limits=copy.deepcopy(acceptance.LIMITS),
            thresholds=copy.deepcopy(acceptance.THRESHOLDS),
            cases=copy.deepcopy(acceptance.CASES), eos_qualified=False,
            audio_qualified=False, encoder_qualified=False)
        for case in self.plan['cases']:
            case.update(speaker_embedding=str(vector),
                        speaker_embedding_sha256=acceptance.digest(vector),
                        reference_codes=str(codes),
                        reference_codes_sha256=acceptance.digest(codes),
                        reference_samples=acceptance.REFERENCE_SAMPLES,
                        reference_text=acceptance.REFERENCE_TEXT)

    def test_valid_complete_owned_reference_and_two_alignments(self):
        acceptance.validate_plan(self.plan)
        codes = acceptance.read_reference(self.plan['cases'][0])
        self.assertEqual(len(codes), 30)
        self.assertEqual(codes[-1], list(range(464, 480)))

    def test_code_corruption_shape_and_reserved_ids_rejected(self):
        path = Path(self.plan['cases'][0]['reference_codes'])
        original = path.read_bytes()
        for data, resign in ((original[:-4], True),
                             (original + bytes(64), True),
                             (struct.pack('<i', -1) + original[4:], True),
                             (struct.pack('<i', 2048) + original[4:], True),
                             (struct.pack('<i', 1) + original[4:], False)):
            path.write_bytes(data)
            if resign:
                for row in self.plan['cases']:
                    row['reference_codes_sha256'] = acceptance.digest(path)
            with self.assertRaises(AssertionError):
                acceptance.validate_plan(self.plan)
            path.write_bytes(original)
            for row in self.plan['cases']:
                row['reference_codes_sha256'] = acceptance.digest(path)

    def test_missing_changed_or_fractional_reference_rejected(self):
        for key, value in (('reference_codes', '/nonexistent/codes'),
                           ('reference_text', ''), ('reference_text', 'wrong'),
                           ('reference_samples', 55693.0),
                           ('reference_samples', 55694)):
            plan = copy.deepcopy(self.plan)
            for row in plan['cases']:
                row[key] = value
            with self.assertRaises(AssertionError):
                acceptance.validate_plan(plan)

    def test_zero_nonfinite_or_truncated_speaker_rejected(self):
        path = Path(self.plan['cases'][0]['speaker_embedding'])
        original = path.read_bytes()
        for data in (bytes(8192), original[:-4],
                     struct.pack('<f', float('nan')) + original[4:]):
            path.write_bytes(data)
            for row in self.plan['cases']:
                row['speaker_embedding_sha256'] = acceptance.digest(path)
            with self.assertRaises(AssertionError):
                acceptance.validate_plan(self.plan)
        path.write_bytes(original)

    def test_changed_alignment_budget_gate_or_quality_claim_rejected(self):
        mutations = [lambda p: p['cases'][0].update(non_streaming=True),
            lambda p: p['limits'].update(gpu_model_loads=2),
            lambda p: p['thresholds']['dense'].update(relative_l2=1),
            lambda p: p.update(encoder_qualified=True),
            lambda p: p.update(eos_qualified=True),
            lambda p: p.update(audio_qualified=True)]
        for mutate in mutations:
            plan = copy.deepcopy(self.plan)
            mutate(plan)
            with self.assertRaises(AssertionError):
                acceptance.validate_plan(plan)

    def inputs(self):
        return dict(voice_clone_prompt=dict(ref_spk_embedding=[object()],
                    x_vector_only_mode=[True], icl_mode=[False], ref_code=None),
                    non_streaming_mode=False, languages=['english'],
                    input_ids=object(), instruct_ids=object())

    def test_adapter_preserves_speaker_text_instruction_and_source_owner(self):
        before = self.inputs()
        ids, codes = object(), object()
        result = apply_icl_inputs(before, self.plan['cases'][0], ids, codes)
        self.assertIs(result['input_ids'], before['input_ids'])
        self.assertIs(result['instruct_ids'], before['instruct_ids'])
        self.assertIs(result['voice_clone_prompt']['ref_spk_embedding'],
                      before['voice_clone_prompt']['ref_spk_embedding'])
        self.assertEqual(result['ref_ids'], [ids])
        self.assertEqual(result['voice_clone_prompt']['ref_code'], [codes])
        self.assertEqual(result['voice_clone_prompt']['icl_mode'], [True])
        self.assertEqual(result['voice_clone_prompt']['x_vector_only_mode'], [False])
        self.assertNotIn('ref_ids', before)
        self.assertIsNone(before['voice_clone_prompt']['ref_code'])
        self.assertEqual(before['voice_clone_prompt']['icl_mode'], [False])

    def test_adapter_rejects_drift_in_expected_reference_call(self):
        for mutate in [lambda p: p.update(ref_ids=[]),
                       lambda p: p.update(non_streaming_mode=True),
                       lambda p: p.update(languages=['chinese']),
                       lambda p: p['voice_clone_prompt'].update(icl_mode=[True]),
                       lambda p: p['voice_clone_prompt'].update(ref_code=[]),
                       lambda p: p['voice_clone_prompt'].update(ref_spk_embedding=[])]:
            kwargs = self.inputs()
            mutate(kwargs)
            with self.assertRaises(AssertionError):
                apply_icl_inputs(kwargs, self.plan['cases'][0], object(), object())


if __name__ == '__main__':
    unittest.main()
