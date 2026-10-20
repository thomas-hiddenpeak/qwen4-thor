"""Bounded cache preparation must preserve a strict observed-zero gate."""
import unittest
from unittest import mock
import run_budget_experiment as budget


def observation(residual):
    return dict(complete_file_set_observed=residual is not None,
                files=[] if residual is None else
                [dict(path='/model/payload.safetensors', resident_bytes=residual)])


class CachePreparationTest(unittest.TestCase):
    def run_case(self, residuals, errors=None):
        with mock.patch.object(budget, 'clear_target_cache', return_value=errors or []) as clear, \
             mock.patch.object(budget, 'observe_files', side_effect=list(map(observation, residuals))), \
             mock.patch.object(budget.time, 'sleep'):
            result = budget.prepare_target_cache(['/model/payload.safetensors'])
        return result, clear.call_count

    def test_already_clear_stops_after_one(self):
        (_, after, attempts), count = self.run_case([0])
        self.assertEqual((count, len(attempts), budget.payload_residual(after)), (1, 1, 0))

    def test_transient_residency_preserved_then_zero(self):
        (_, after, attempts), count = self.run_case([393216, 0])
        self.assertEqual(count, 2)
        self.assertEqual(attempts[0]['payload_resident_bytes'], 393216)
        self.assertEqual(budget.payload_residual(after), 0)

    def test_persistent_residency_cannot_pass(self):
        (_, after, attempts), count = self.run_case([4096, 4096, 4096])
        self.assertEqual((count, len(attempts)), (3, 3))
        self.assertGreater(budget.payload_residual(after), 0)

    def test_unknown_is_not_retried(self):
        (_, after, _), count = self.run_case([None])
        self.assertEqual(count, 1)
        self.assertIsNone(budget.payload_residual(after))

    def test_advice_error_not_retried_even_if_zero(self):
        (errors, _, _), count = self.run_case([0], errors=[{'error': 'denied'}])
        self.assertEqual(count, 1)
        self.assertTrue(errors)

    def test_invalid_budget_rejected_before_io(self):
        for value in (0, 4):
            with self.assertRaises(ValueError):
                budget.prepare_target_cache([], max_passes=value)


if __name__ == '__main__':
    unittest.main()
