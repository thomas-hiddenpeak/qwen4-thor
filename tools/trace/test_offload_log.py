"""Independent finite-set checks for diagnostic-derived load bounds."""
import itertools
import os
from pathlib import Path
import tempfile
import unittest

from analyze_offload_log import analyze_log, transition_bound


class LogBoundsTest(unittest.TestCase):
    def setUp(self):
        self.output = Path(os.environ.get(
            'Q4T_TEST_OUTPUT_DIR',
            Path(__file__).resolve().parents[2] / 'build/offload-log-contracts'))
        self.output.mkdir(parents=True, exist_ok=True)

    def test_bound_matches_exhaustive_feasible_resident_sets(self):
        universe = set(range(5))
        capacity = 3
        sets = [set(x) for size in range(1, capacity + 1)
                for x in itertools.combinations(universe, size)]
        caches = [set(x) for x in itertools.combinations(universe, capacity)]
        for previous in sets:
            for needed in sets:
                exact = min(len(needed - cache) for cache in caches
                            if previous <= cache)
                self.assertEqual(exact, transition_bound(
                    len(previous), len(needed - previous), capacity))

    def test_prefill_singleton_uses_runtime_decode_counter(self):
        log = '''[q4t][residency][diag] layer=0 flush size=2 book=3 actual=3
[q4t][residency][diag] layer=0 flush size=1 book=2 actual=2
[q4t][residency][diag] layer=0 T=3 D=3 chunks=2 max_distinct=3 resident_before=3 overlap=1 new=1
[q4t][residency] id=r1 finish=length in=3 out=1 pmiss=2 dmiss=1
'''
        with tempfile.TemporaryDirectory(dir=self.output) as tmp:
            path = Path(tmp) / 'server.log'
            path.write_text(log)
            row = analyze_log(path)['requests'][0]
            self.assertEqual(row['transition_lower_bounds'],
                             {'runtime_prefill': 0, 'runtime_decode': 1})
            path.write_text(log.replace('dmiss=1', 'dmiss=0'))
            with self.assertRaises(ValueError):
                analyze_log(path)

    def test_incomplete_diagnostics_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=self.output) as tmp:
            path = Path(tmp) / 'server.log'
            path.write_text('[q4t][residency][diag] layer=0 flush '
                            'size=2 book=3 actual=3\n')
            with self.assertRaises(ValueError):
                analyze_log(path)


if __name__ == '__main__':
    unittest.main()
