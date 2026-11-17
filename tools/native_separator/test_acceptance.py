"""Negative controls for the new waveform separation acceptance checks."""
import unittest

import numpy as np

from tools.native_separator.acceptance import separation_quality, stage_counts


class SeparationAcceptanceTests(unittest.TestCase):
    def sources(self):
        x = np.arange(16000, dtype=np.float64)
        return np.stack([np.sin(x*.173)*.2, np.cos(x*.371)*.3])

    def test_source_permutation_and_scale_preserve_both_actual_signals(self):
        sources = self.sources()
        result = separation_quality(sources, sources[::-1]*.7)
        self.assertTrue(result['passed'])
        self.assertEqual(result['permutation'], [1, 0])
        self.assertTrue(all(x > 50 for x in result['improvement_db']))

    def test_passthrough_and_missing_second_voice_cannot_pass(self):
        sources = self.sources()
        mixed = sources.sum(axis=0)
        for output in [np.stack([mixed, mixed]),
                       np.stack([sources[0], np.zeros(16000)]),
                       np.stack([sources[0], sources[0]])]:
            with self.subTest():
                self.assertFalse(separation_quality(sources, output)['passed'])

    def test_invalid_or_empty_source_evidence_is_rejected(self):
        sources = self.sources()
        for a, b in [(sources, sources[:, :-1]),
                     (sources[0], sources),
                     (np.zeros_like(sources), sources),
                     (sources, np.full_like(sources, np.nan)),
                     (np.full_like(sources, np.inf), sources)]:
            with self.subTest(), self.assertRaises(ValueError):
                separation_quality(a, b)

    def test_trace_counts_cover_full_input_and_all_paths(self):
        self.assertEqual(stage_counts(16), [256]+[128000]*4+[512, 32])
        self.assertEqual(stage_counts(257)[-2:], [15872, 514])
        self.assertEqual(stage_counts(64000),
                         [2047744]+[4224000]*4+[4095488, 128000])
        for value in [15, 64001, 16.5, None]:
            with self.subTest(), self.assertRaises(ValueError):
                stage_counts(value)


if __name__ == '__main__':
    unittest.main()
