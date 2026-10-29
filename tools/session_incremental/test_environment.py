"""Bounded CPU regression for the arm-wiring failure; no inference or IO load."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('incremental_runner',
    Path(__file__).with_name('run_stage.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class EnvironmentContract(unittest.TestCase):
    def test_all_four_frozen_arms(self):
        for head, value in [('full',0),('full',1),('subset',1),('subset',0)]:
            with self.subTest(head=head, incremental=value):
                step = dict(head=head, incremental=value, pipeline=0, preallocate=0)
                with mock.patch.object(runner.legacy, 'clean_env', return_value={
                        'Q4T_SESSION_INCREMENTAL':'wrong', 'Q4T_SESSION_PIPELINE':'1'}):
                    env = runner.environment(step)
                self.assertEqual(env['Q4T_SESSION_INCREMENTAL'], str(value))
                self.assertEqual(env['Q4T_SESSION_PIPELINE'], '0')
                self.assertEqual(env['Q4T_SESSION_PREALLOCATE'], '0')
                self.assertEqual(env['Q4T_SESSION_SAVE_TIMING'], '1')

    def test_invalid_or_mixed_candidate_rejected(self):
        for incremental, pipeline in [(2,0),(0,1),(1,1)]:
            with self.subTest(incremental=incremental,pipeline=pipeline):
                with self.assertRaises(ValueError):
                    runner.environment(dict(incremental=incremental,pipeline=pipeline,preallocate=0))


if __name__ == '__main__':
    unittest.main()
