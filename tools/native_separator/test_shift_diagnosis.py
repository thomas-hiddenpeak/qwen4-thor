"""Channel, polarity and rejection controls before the bounded GPU diagnosis."""
import unittest
import numpy as np

from tools.native_separator.shift_diagnosis import aligned_average


class ShiftAlignment(unittest.TestCase):
    def setUp(self):
        rng=np.random.default_rng(1947)
        self.base=rng.normal(size=(2,32000)).astype('<f4')

    def test_swapped_scaled_inverted_channels_keep_both_real_waves(self):
        shifted=self.base[[1,0]]*np.array([[-3.],[.25]])
        before=self.base.copy()
        result,record=aligned_average(self.base,shifted)
        np.testing.assert_array_equal(result,self.base)
        np.testing.assert_array_equal(before,self.base)
        self.assertEqual(record['permutation'],[1,0])
        np.testing.assert_allclose(record['gains'],[4.,-1/3],atol=1e-12)

    def test_unrelated_shift_cannot_be_mixed_into_a_speaker(self):
        shifted=np.random.default_rng(17).normal(size=self.base.shape)
        with self.assertRaisesRegex(ValueError,'ambiguous'):aligned_average(self.base,shifted)

    def test_bad_shape_silent_and_nonfinite_sources_rejected(self):
        for bad in [self.base[:1],self.base[:,:-1],np.zeros_like(self.base),
                    np.full_like(self.base,np.nan),np.full_like(self.base,np.inf)]:
            with self.assertRaises(ValueError):aligned_average(self.base,bad)


if __name__=='__main__':unittest.main()
