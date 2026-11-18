"""Fixed-reference full-audio gate checks; no acoustic model or GPU calls."""
import copy
import unittest

from . import icl_synthesis_acceptance as gate
from . import test_icl_acceptance as fixtures


class IclSynthesisGate(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.IclAcceptanceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.plan = dict(schema='q4t-native-icl-synthesis-v1',
            limits=copy.deepcopy(gate.LIMITS), contract=copy.deepcopy(gate.CONTRACT),
            cases=fixture.plan['cases'])
        self.receipt = dict(status='PASS_ICL_SYNTHESIS_PENDING_CONTENT',
            allocated=12, released=12, gpu_talker_loads=1, gpu_codec_loads=1,
            asset_workers_reaped=2, cases=2, cancellation_and_two_frame_limits=True,
            advances=25000, sampling_policy='checkpoint-default-seed0', encoder_loads=0)
        self.audio = dict(early_output=True, deferred=False, eos=True,
            at_limit=False, reference_frames=30, frames=35, samples=35 * 1920,
            sample_rate=24000, chunks=[2, 16, 16, 1])

    def test_fixed_reference_and_production_sampling_plan(self):
        gate.validate_plan(self.plan)
        gate.validate_native(self.receipt)

    def test_extra_load_or_relaxed_content_gate_rejected(self):
        for mutate in [lambda p: p['limits'].update(gpu_reference_encoder_loads=1),
                       lambda p: p['contract'].update(exact_normalized_transcription=False),
                       lambda p: p['contract'].update(sampling='greedy'),
                       lambda p: p['contract'].update(registered_voice_api_qualified=True)]:
            p = copy.deepcopy(self.plan)
            mutate(p)
            with self.assertRaises(AssertionError):
                gate.validate_plan(p)

    def test_missing_cleanup_cancel_or_budget_result_rejected(self):
        for k, value in [('released', 11), ('allocated', 10), ('asset_workers_reaped', 1),
                         ('cancellation_and_two_frame_limits', False),
                         ('encoder_loads', 1), ('advances', 100001),
                         ('sampling_policy', 'greedy')]:
            receipt = dict(self.receipt, **{k: value})
            with self.assertRaises(AssertionError):
                gate.validate_native(receipt)

    def test_incremental_mixed_first_chunk_and_last_tail(self):
        gate.validate_chunks(self.audio, 0)

    def test_deferred_requires_eos_before_any_output(self):
        gate.validate_chunks(dict(self.audio, early_output=False, deferred=True), 1)
        with self.assertRaises(AssertionError):
            gate.validate_chunks(dict(self.audio, deferred=True), 1)

    def test_reference_leak_drop_overlap_or_rechunk_is_rejected(self):
        for changes in [dict(samples=65 * 1920), dict(reference_frames=0),
                        dict(chunks=[16, 16, 3]), dict(chunks=[2, 15, 16, 2]),
                        dict(chunks=[2, 16, 16, 2]), dict(chunks=[2, 16, 16, True]),
                        dict(chunks=[2, 16, 17]), dict(frames=35.0)]:
            with self.assertRaises(AssertionError):
                gate.validate_chunks(dict(self.audio, **changes), 0)

    def test_exhausted_frames_never_count_as_success(self):
        for changes in [dict(eos=False), dict(at_limit=True), dict(frames=160),
                        dict(chunks=[2]), dict(frames=2), dict(sample_rate=16000)]:
            with self.assertRaises(AssertionError):
                gate.validate_chunks(dict(self.audio, **changes), 0)

    def test_full_audio_reference_leak_truncation_and_wrong_language_fail(self):
        case = self.plan['cases'][0]
        self.assertTrue(gate.content_check('language English<asr_text>' +
                                          case['text'], case)['passed'])
        for text in ['The blue train arrives at noon. ' + case['text'],
                     'Hello.', 'No speech', '']:
            self.assertFalse(gate.content_check('language English<asr_text>' +
                                               text, case)['passed'])
        with self.assertRaises(AssertionError):
            gate.content_check('language Chinese<asr_text>' + case['text'], case)


if __name__ == '__main__':
    unittest.main()
