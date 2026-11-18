"""Keep original speech samples, graph coverage and content gates mandatory."""
import copy
import os
from pathlib import Path
import struct
import tempfile
import unittest

from . import base_utterance_acceptance as a


class BaseUtteranceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_TMP'])
        self.addCleanup(self.temp.cleanup)
        self.plan = dict(schema='q4t-native-base-tts-utterance-v1',
            limits=copy.deepcopy(a.LIMITS), contract=copy.deepcopy(a.CONTRACT),
            cases=copy.deepcopy(a.component.CASES))
        for i, row in enumerate(self.plan['cases']):
            for key, count in (('reference_pcm', 1280), ('speaker_embedding', 2048)):
                path = Path(self.temp.name) / f'{i}-{key}.f32'
                path.write_bytes(struct.pack('<' + str(count) + 'f',
                                            *([0.1 * (i + 1)] * count)))
                row[key], row[key + '_sha256'] = str(path), a.digest(path)

    def test_real_pipeline_receipt_requires_speaker_graph_and_both_cases(self):
        row = dict(status='PASS_UTTERANCE_PENDING_CONTENT_CHECK',
                   gpu_model_loads=3, cases=2, speaker_stages=94, frames=100,
                   generation_advances=15000, codec_advances=1000)
        a.validate_native(row)
        for key, value in (('gpu_model_loads', 2), ('speaker_stages', 47),
                           ('cases', 1), ('frames', 320),
                           ('generation_advances', 48001)):
            bad = dict(row, **{key: value})
            with self.assertRaises(AssertionError):
                a.validate_native(bad)

    def test_distinct_source_samples_cannot_be_substituted_or_truncated(self):
        a.validate_plan(self.plan)
        bad = copy.deepcopy(self.plan)
        for key in ('reference_pcm', 'reference_pcm_sha256'):
            bad['cases'][1][key] = bad['cases'][0][key]
        with self.assertRaises(AssertionError):
            a.validate_plan(bad)
        path = Path(self.plan['cases'][0]['reference_pcm'])
        path.write_bytes(path.read_bytes()[:-4])
        with self.assertRaises(AssertionError):
            a.validate_plan(self.plan)

    def test_exact_content_eos_budget_and_unqualified_api_claim_remain_fixed(self):
        for key, value in (('natural_eos', False),
                           ('exact_normalized_transcription', False),
                           ('registered_voice_api_qualified', True)):
            bad = copy.deepcopy(self.plan)
            bad['contract'][key] = value
            with self.assertRaises(AssertionError):
                a.validate_plan(bad)
        bad = copy.deepcopy(self.plan)
        bad['limits']['gpu_speaker_loads'] = 2
        with self.assertRaises(AssertionError):
            a.validate_plan(bad)


if __name__ == '__main__':
    unittest.main()
