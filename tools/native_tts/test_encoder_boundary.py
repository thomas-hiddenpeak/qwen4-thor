"""Positive and adversarial RVQ-boundary tests; no model/CUDA calls."""
import copy
import os
from pathlib import Path
import tempfile
import unittest

import numpy as np

from .encoder_boundary import classify_trajectory, continuous_pair, nearest
from .encoder_boundary_review import different_frames, verify_recorded_files
from .encoder_acceptance import digest


def fixture(left=1 + 2**-20, right=1 - 2**-20):
    books = [np.array([[0, 0], [2, 0], [5, 0]], '<f4') for _ in range(2)]
    books.append(np.array([[0, 0], [.5, 0], [-.5, 0]], '<f4'))
    projections = [np.array([[.25, 0], [value, 0]], '<f4')
                   for value in (left, right)]
    codes, residuals = [], []
    for projection in projections:
        selected, rows = [], []
        for group, book in enumerate(books):
            value = projection[group] if group < 2 else rows[-1]
            distance = np.sum((book.astype('float64') - value)**2, axis=1)
            code = int(np.argmin(distance))
            rows.append(np.subtract(value, book[code], dtype=np.float32))
            selected.append(code)
        codes.append(np.array(selected, '<i4'))
        residuals.append(np.array(rows, '<f4'))
    return books, projections, residuals, codes


class EncoderBoundaryTests(unittest.TestCase):
    def test_exact_nearest_requires_no_exception_to_code_equality(self):
        result = classify_trajectory(*fixture(.8, .8))
        self.assertTrue(all(r['classification'] == 'SAME_CODE_ON_VALID_OWN_INPUT'
                            for r in result))

    def test_small_crossing_and_causal_residual_are_separately_proved(self):
        result = classify_trajectory(*fixture())
        self.assertEqual([r['classification'] for r in result],
            ['SAME_CODE_ON_VALID_OWN_INPUT', 'BOUNDED_FLOAT_BOUNDARY_CROSSING',
             'EXACT_CAUSAL_RESIDUAL_BRANCH'])
        self.assertLess(result[1]['continuous_input']['relative_l2'], .001)
        self.assertGreater(result[1]['boundary']['native_gap'], 0)
        self.assertLess(result[1]['boundary']['reference_gap'], 0)

    def test_wrong_nearest_code_cannot_hide_behind_small_input_error(self):
        args = fixture()
        args[3][0][1] = 0
        args[2][0][1] = args[1][0][1] - args[0][1][0]
        with self.assertRaisesRegex(ValueError, 'not independently nearest'):
            classify_trajectory(*args)

    def test_large_correct_nearest_change_fails_original_float_gate(self):
        with self.assertRaisesRegex(ValueError, 'original continuous gate'):
            classify_trajectory(*fixture(1.2, .8))

    def test_later_non_nearest_is_not_excused_by_valid_prior_branch(self):
        args = fixture()
        args[3][0][2] = 0
        args[2][0][2] = args[2][0][1] - args[0][2][0]
        with self.assertRaisesRegex(ValueError, 'not independently nearest'):
            classify_trajectory(*args)

    def test_single_bit_residual_corruption_is_rejected(self):
        for group in range(3):
            args = fixture()
            args[2][0][group].view(np.uint32)[0] ^= 1
            with self.assertRaisesRegex(ValueError, 'exact own-code'):
                classify_trajectory(*args)

    def test_ambiguous_midpoint_and_duplicate_centroids_do_not_pass(self):
        with self.assertRaisesRegex(ValueError, 'numerically ambiguous'):
            classify_trajectory(*fixture(1, 1))
        with self.assertRaisesRegex(ValueError, 'numerically ambiguous'):
            nearest(np.zeros((2, 2)), np.zeros(2), 0)

    def test_nonfinite_and_wrong_shape_or_dtype_are_rejected(self):
        for mutate in [lambda a: a[0][0].__setitem__((0, 0), np.nan),
                       lambda a: a[1][0].__setitem__((0, 0), np.inf),
                       lambda a: a[2][0].__setitem__((0, 0), np.nan),
                       lambda a: a[3][0].__setitem__(0, -1),
                       lambda a: a[3][0].__setitem__(0, 3),
                       lambda a: a[1].__setitem__(0, np.zeros((1, 2), '<f4')),
                       lambda a: a[2].__setitem__(0, a[2][0].astype('float64')),
                       lambda a: a[3].__setitem__(0, a[3][0].astype('float32'))]:
            args = fixture()
            mutate(args)
            with self.assertRaises(ValueError):
                classify_trajectory(*args)

    def test_all_centroids_are_checked_not_only_reported_top_two(self):
        book = np.array([[0., 0.], [2., 0.], [1.1, 0.]])
        with self.assertRaisesRegex(ValueError, 'not independently nearest'):
            nearest(book, np.array([1.01, 0.]), 1)

    def test_acoustic_reset_cannot_inherit_semantic_branch_permission(self):
        books, projections, residuals, codes = fixture(1.2, .8)
        # Create a valid tiny semantic crossing while acoustic input still
        # violates the original float gate at its independent reset.
        for side, value in enumerate((1 + 2**-20, 1 - 2**-20)):
            projections[side][0] = [value, 0]
            codes[side][0] = 1 - side
            residuals[side][0] = projections[side][0] - books[0][codes[side][0]]
        with self.assertRaisesRegex(ValueError, 'original continuous gate'):
            classify_trajectory(books, projections, residuals, codes)

    def test_exact_zero_and_opposite_vectors_keep_original_float_gate(self):
        continuous_pair(np.zeros(2), np.zeros(2))
        for other in (np.ones(2), -np.ones(2)):
            with self.assertRaises(ValueError):
                continuous_pair(np.zeros(2), other)
        with self.assertRaises(ValueError):
            continuous_pair(np.array([1., 0.]), np.array([-1., 0.]))

    def test_classifier_never_changes_input_codes_or_operands(self):
        args = fixture()
        before = copy.deepcopy(args)
        classify_trajectory(*args)
        for left, right in zip(args, before):
            for a, b in zip(left, right):
                self.assertEqual(a.tobytes(), b.tobytes())

    def test_changed_or_incomplete_artifacts_cannot_be_reused(self):
        with tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_TMP']) as name:
            root = Path(name)
            path = root / 'operand.f32'
            path.write_bytes(b'original data')
            records = {path.name: digest(path)}
            verify_recorded_files(root, records)
            path.write_bytes(b'changed data')
            for record in (records, {}, {'../operand.f32': 'bad'},
                           {'/tmp/operand.f32': 'bad'}):
                with self.assertRaises(ValueError):
                    verify_recorded_files(root, record)

    def test_different_frames_require_their_own_complete_trace(self):
        left = np.zeros((100, 16), '<i4')
        right = left.copy()
        left[94, 11] = 3
        self.assertEqual(different_frames(left, right, 100, [94]), [94])
        left[95, 2] = 1
        with self.assertRaisesRegex(ValueError, 'no complete captured'):
            different_frames(left, right, 100, [94])
        left[95, 2] = -1
        with self.assertRaisesRegex(ValueError, 'invalid recorded'):
            different_frames(left, right, 100, [94])


if __name__ == '__main__':
    unittest.main()
