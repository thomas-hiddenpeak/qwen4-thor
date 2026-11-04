"""Actual failed metric wire must compare by complete series, not map order."""
import unittest

from tools.serve_host import projection_fix as f


class Projection(unittest.TestCase):
    def setUp(self):
        base=f.PREVIOUS/'observer';self.raw=(base/'metrics.prom').read_bytes()
        self.value=f.pkg.read(base/'status.json');self.digest=f.pkg.sha(base/'policy.json')

    def test_original_valid_wire_matches_after_json_sorted_event_keys(self):
        expected=f.s.o.c.metrics(self.value,'thor-host-short',self.digest).encode()
        self.assertNotEqual(self.raw,expected)
        self.assertTrue(f.s.projection_matches(self.raw,self.value,'thor-host-short',self.digest))

    def test_missing_duplicate_and_changed_series_are_still_rejected(self):
        lines=self.raw.splitlines(True);series=next(v for v in lines if b'event="high"' in v)
        for raw in (self.raw.replace(series,b''),self.raw+series,self.raw.replace(series,series.replace(b'} 0',b'} 1'))):
            with self.subTest(raw=raw[-80:]):
                self.assertFalse(f.s.projection_matches(raw,self.value,'thor-host-short',self.digest))

    def test_cleanup_launch_and_business_methods_are_unchanged(self):
        original=(f.ORIGINAL/'cpu-driver-02/source/tools/serve_host/stage.py').read_bytes()
        self.assertEqual(f.method_contract(original),f.method_contract((f.ROOT/'tools/serve_host/stage.py').read_bytes()))
        self.assertLessEqual(f.GPU_WINDOW+(f.pkg.read(f.PREVIOUS/'execution/result.json')['gpu_finished']-
            f.pkg.read(f.PREVIOUS/'execution/result.json')['gpu_started']),210)


if __name__=='__main__':unittest.main()
