"""Offline guardrails for failure evidence and the fixed execution budget."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.serve_backend_cleanup import verify as v


class Contracts(unittest.TestCase):
    def test_unhandled_exit_cannot_be_accepted_as_success(self):
        log = '[cleanup-test] api=cudaFree phase=probe error=1\n[q4t][cleanup-fatal]\n'
        args = dict(expected=1, api='cudaFree', phase='probe', production=False)
        v.probe_outcome(1, '', log, **args)
        for code, stdout, stderr in ((0, '', log), (1, 'cleanup probe PASS:', log),
            (1, '', log.replace('[q4t][cleanup-fatal]', 'ignored')),
            (1, '', log.replace('phase=probe', 'phase=wrong')),
            (1, '', log+log)):
            with self.subTest(code=code, stdout=stdout, stderr=stderr):
                with self.assertRaises(RuntimeError):
                    v.probe_outcome(code, stdout, stderr, **args)

    def test_production_never_accepts_injected_cleanup(self):
        args = dict(expected=0, api='cudaFree', phase='probe', production=True)
        v.probe_outcome(0, 'cleanup probe PASS:', '', **args)
        with self.assertRaises(RuntimeError):
            v.probe_outcome(0, 'cleanup probe PASS:', '[cleanup-test]', **args)

    def test_handled_error_returns_without_fatal_or_fake_success(self):
        log = '[cleanup-test] api=cudaFree phase=probe error=1'
        args = dict(expected=0, api='cudaFree', phase='probe', production=False)
        v.probe_outcome(0, 'cleanup probe PASS:', log, **args)
        for stdout, stderr in (('', log), ('cleanup probe PASS:', log+'[q4t][cleanup-fatal]')):
            with self.assertRaises(RuntimeError): v.probe_outcome(0, stdout, stderr, **args)

    def test_environment_cannot_mix_unrelated_hooks(self):
        with patch.dict(os.environ, {'LD_PRELOAD': 'bad', 'Q4T_SERVE_DEVICE_FAULT': 'other',
                                    'Q4T_CLEANUP_FAULT_API': 'other'}):
            clean = v.environment()
            self.assertNotIn('LD_PRELOAD', clean)
            self.assertFalse(any(k.startswith('Q4T_') for k in clean))
            env = v.environment('cudaFree', 'model.release')
            self.assertNotIn('Q4T_SERVE_DEVICE_FAULT', env)
            self.assertEqual(env['Q4T_CLEANUP_FAULT_PHASE'], 'model.release')
            env = v.environment(legacy=True)
            self.assertEqual(env['Q4T_SERVE_DEVICE_FAULT'], 'startup.mtp_load')
            self.assertNotIn('Q4T_CLEANUP_FAULT_API', env)

    def test_exact_scope_and_no_hidden_retry_budget(self):
        rows = v.probe_cases()
        self.assertEqual(len(rows), 17)
        self.assertEqual(len({r[0] for r in rows}), 17)
        self.assertEqual(sum(r[4] == 1 for r in rows), 11)
        self.assertEqual({r[2] for r in rows if r[0] in v.APIS}, set(v.APIS))
        self.assertEqual(len(v.SCENARIOS), v.stage.PLANNED['starts'])
        self.assertEqual(sum(r[3] == 1 for r in v.SCENARIOS), v.stage.PLANNED['negatives'])
        self.assertEqual(5+len(v.SCENARIOS)-2, v.stage.PLANNED['generations'])

    def test_budget_refuses_before_extra_work(self):
        with tempfile.TemporaryDirectory(dir=v.ROOT/'.q4t-work') as directory:
            run = object.__new__(v.Run)
            run.out, run.deadline = Path(directory), time.time()+30
            run.counts = dict(v.stage.PLANNED)
            run.counts['starts'] -= 1
            run.budget('starts')
            with self.assertRaises(RuntimeError): run.budget('starts')
            self.assertEqual(len(list(run.out.glob('attempt*'))), 1)
            run.deadline = 0
            with self.assertRaises(RuntimeError): run.budget()


if __name__ == '__main__': unittest.main()
