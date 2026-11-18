"""Direct negative controls for complete EOS/audio and real content checks."""
import copy
from pathlib import Path
import tempfile
import unittest

import numpy as np

from tools.native_tts import utterance_acceptance as gate


class UtteranceAcceptanceTest(unittest.TestCase):
    def test_content_cannot_drop_missing_words_or_wrong_language(self):
        for case in gate.CASES:
            prefix = 'language ' + case['language'].capitalize() + '<asr_text>'
            self.assertTrue(gate.content_check(prefix + case['text'], case)['passed'])
            self.assertFalse(gate.content_check(prefix + 'unrelated words', case)['passed'])
            self.assertFalse(gate.content_check(prefix, case)['passed'])
            with self.assertRaises(AssertionError):
                gate.content_check(case['text'], case)
        plan = dict(schema='q4t-native-tts-utterance-v1', limits=gate.LIMITS,
                    cases=gate.CASES, contract=gate.CONTRACT)
        gate.validate_plan(plan)
        bad = copy.deepcopy(plan)
        bad['limits']['gpu_talker_loads'] = 2
        with self.assertRaises(AssertionError):
            gate.validate_plan(bad)

    def test_wave_cannot_accept_truncation_silence_or_nonfinite(self):
        with tempfile.TemporaryDirectory(dir=gate.ROOT / '.q4t-work') as tmp:
            path = Path(tmp) / 'wave.f32'
            record = dict(frames=2, samples=3840, sample_rate=24000,
                          eos=True, at_limit=False)
            np.full(3840, 0.1, dtype='<f4').tofile(path)
            gate.validate_wave(path, record)
            for field, value in (('eos', False), ('at_limit', True), ('samples', 3839)):
                with self.subTest(field=field), self.assertRaises(AssertionError):
                    gate.validate_wave(path, dict(record, **{field: value}))
            for value in (0, np.nan, np.inf, 1.1):
                np.full(3840, value, dtype='<f4').tofile(path)
                with self.subTest(value=value), self.assertRaises(AssertionError):
                    gate.validate_wave(path, record)
            path.write_bytes(b'')
            with self.assertRaises(AssertionError):
                gate.validate_wave(path, record)


if __name__ == '__main__':
    unittest.main()
