"""Driver refusal/accounting checks; no listeners, subprocesses or models."""
import unittest
from unittest.mock import patch

from tools.serve_client_transport import stage as s


class Budget(unittest.TestCase):
    def run_object(self):
        run = object.__new__(s.Run)
        run.started = 0
        run.counts = dict(starts=0,generation_attempts=0,controls=0,network=0)
        return run

    def test_no_seventh_generation_is_admitted(self):
        run = self.run_object()
        with patch.object(s.time,'monotonic',return_value=1),patch.object(s.pkg,'save'):
            for _ in range(6):
                run.budget('generation_attempts')
            with self.assertRaises(RuntimeError):
                run.budget('generation_attempts')
        self.assertEqual(run.counts['generation_attempts'],6)

    def test_work_stops_before_reserved_cleanup_window(self):
        run = self.run_object()
        with patch.object(s.time,'monotonic',return_value=s.WORK_SECONDS),patch.object(s.pkg,'save') as save:
            with self.assertRaises(RuntimeError):
                run.budget('starts')
        save.assert_not_called()
        self.assertEqual(run.counts['starts'],0)

    def test_incomplete_business_cannot_pass(self):
        counts = dict(starts=1,generation_attempts=6,controls=3,network=2*len(s.probes.CASES))
        s.verify_counts(counts,6,2)
        for success,tools in ((5,2),(6,1),(6,3)):
            with self.assertRaises(RuntimeError):
                s.verify_counts(counts,success,tools)
