import unittest
from run_stage import ROOT
from audit import runtime


class RuntimeAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = (ROOT / '.q4t-work/session-cache-20261027/phase-a-remaining/run/subset-http/server.log').read_text()

    def test_completed_zero_hit_lazy_service(self):
        self.assertTrue(runtime(self.raw, 18)['passed'])

    def test_original_full_partial_step(self):
        raw = (ROOT / '.q4t-work/session-cache-20261027/phase-a/run/full-http/server.log').read_text()
        self.assertTrue(runtime(raw, 17)['passed'])

    def test_missing_release(self):
        with self.assertRaises(RuntimeError):
            runtime(self.raw.replace('released=1', 'released=0', 1), 18)

    def test_eager_or_missing_table(self):
        with self.assertRaises(RuntimeError):
            runtime(self.raw.replace('event=configure', 'event=init', 1), 18)

    def test_bad_attribution(self):
        with self.assertRaises(RuntimeError):
            runtime(self.raw.replace('checks=0 lookups=0', 'checks=1 lookups=0'), 18)

    def test_io_failure(self):
        with self.assertRaises(RuntimeError):
            runtime(self.raw.replace('error=none', 'error=checksum', 1), 18)


if __name__ == '__main__':
    unittest.main()
