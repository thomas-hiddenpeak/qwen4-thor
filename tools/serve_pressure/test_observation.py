"""Never present a missing kernel OOM counter as zero."""
import unittest

from tools.serve_pressure import stage
from tools.serve_pressure.observe import check_oom


class OomGate(unittest.TestCase):
    def test_missing_or_nonzero_rejected(self):
        good = dict(oom=0, oom_kill=0, oom_group_kill=0)
        check_oom(good)
        for key in good:
            for missing in (True, False):
                value = dict(good)
                if missing:
                    del value[key]
                else:
                    value[key] = 1
                with self.assertRaises(RuntimeError):
                    check_oom(value)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(OomGate)
    with (stage.BASE/'cpu/oom-gate.log').open('x') as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    stage.h.save(stage.BASE/'cpu/oom-gate.json', dict(passed=result.wasSuccessful(), tests=result.testsRun))
    raise SystemExit(0 if result.wasSuccessful() else 1)
