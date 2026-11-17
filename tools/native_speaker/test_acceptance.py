"""Negative controls for fixed speaker numeric acceptance; no model/GPU calls."""
import unittest
import numpy as np
from acceptance import compare_arrays, THRESHOLDS


class AcceptanceTests(unittest.TestCase):
    def test_exact_and_small_rounding_pass(self):
        values = np.linspace(-2, 2, 192).astype('f4')
        self.assertTrue(compare_arrays(values, values, 192, THRESHOLDS['embedding'])['passed'])
        changed = values.copy()
        changed[10] += 1e-6
        self.assertTrue(compare_arrays(changed, values, 192, THRESHOLDS['embedding'])['passed'])

    def test_wrong_shape_and_nan_cannot_pass(self):
        values = np.ones(192)
        with self.assertRaisesRegex(ValueError, 'shape'):
            compare_arrays(values[:-1], values, 192, THRESHOLDS['stage'])
        values[3] = np.nan
        with self.assertRaisesRegex(ValueError, 'nonfinite'):
            compare_arrays(values, values, 192, THRESHOLDS['stage'])

    def test_changed_embeddings_fail_even_with_high_cosine(self):
        values = np.ones(192)
        self.assertFalse(compare_arrays(values*1.1, values, 192, THRESHOLDS['embedding'])['passed'])

    def test_local_outlier_cannot_hide_in_large_norm(self):
        values = np.full(192, 1e4)
        changed = values.copy()
        changed[3] += 0.05
        self.assertFalse(compare_arrays(changed, values, 192, THRESHOLDS['stage'])['passed'])

    def test_silence_zero_norm_and_feature_threshold_are_explicit(self):
        values = np.zeros(192)
        self.assertTrue(compare_arrays(values, values, 192, THRESHOLDS['stage'])['passed'])
        self.assertFalse(compare_arrays(np.ones(192), values, 192, THRESHOLDS['stage'])['passed'])
        self.assertFalse(compare_arrays(np.full(192, 0.0021), values, 192,
                         {'max_abs': THRESHOLDS['features_max_abs']})['passed'])


if __name__ == '__main__':
    unittest.main()
