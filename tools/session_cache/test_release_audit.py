"""CPU regression of the exact failed fixture predicate against raw evidence."""
from pathlib import Path
import unittest

from run_stage import ROOT, released_count


class ReleaseAudit(unittest.TestCase):
    def test_production_log(self):
        log = ROOT / '.q4t-work/session-cache-20261027/phase-a/run/full-http/server.log'
        self.assertEqual(released_count(log.read_text()), 17)

    def test_no_standalone_event(self):
        self.assertEqual(released_count('event=released seq=1\n'), 0)

    def test_missing_and_unreleased(self):
        self.assertEqual(released_count('event=end stage=decode released=0\n'), 0)
        self.assertEqual(released_count('event=admitted released=1\n'), 0)

    def test_success_and_cancel(self):
        self.assertEqual(released_count(
            'event=end stage=response seq=1 released=1 outcome=success\n'
            'event=end stage=decode seq=2 released=1 outcome=cancelled\n'), 2)


if __name__ == '__main__':
    unittest.main()
