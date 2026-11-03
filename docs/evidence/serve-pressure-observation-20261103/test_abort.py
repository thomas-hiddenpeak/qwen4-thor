"""Resource observation failure must remain distinct from business failure."""
from pathlib import Path
import tempfile
import time
import unittest

from tools.serve_pressure import stage
from tools.serve_pressure.observe import Monitor, ObservationAbort
from tools.serve_pressure.verify import Run

OUTPUT = stage.BASE / 'observation-classification'


class Classification(unittest.TestCase):
    def result_for(self, error):
        with tempfile.TemporaryDirectory(dir=OUTPUT) as folder:
            run = Run.__new__(Run)
            run.out, run.started = Path(folder), time.monotonic()
            run.counts, run.preflights = dict(generations=0, negatives=0, starts=0), 0
            def model():
                raise error
            run.model = model
            with self.assertRaises(type(error)):
                run.run()
            result = stage.h.read(run.out/'result.json')
            self.assertEqual(result['counts'], dict(generations=0, negatives=0, starts=0))
            return result

    def test_observation_failure_is_environment_abort(self):
        observer = Monitor.__new__(Monitor)
        observer.error = 'required memory counter missing'
        try:
            observer.check()
        except ObservationAbort as error:
            result = self.result_for(error)
        else:
            self.fail('observer failure was ignored')
        self.assertEqual(result['status'], 'ENV_ABORT')

    def test_business_failure_is_not_environment_abort(self):
        result = self.result_for(RuntimeError('actual response quality failed'))
        self.assertEqual(result['status'], 'FAILED')


if __name__ == '__main__':
    OUTPUT.mkdir(exist_ok=False)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Classification)
    with (OUTPUT/'tests.log').open('x') as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    stage.h.save(OUTPUT/'result.json', dict(passed=result.wasSuccessful(), tests=result.testsRun,
        models_started=0, http_started=0, prior_gpu_evidence_reused=True,
        source_hashes={str(p.relative_to(stage.ROOT)):stage.h.sha(p) for p in (
            stage.ROOT/'tools/serve_pressure/observe.py',
            stage.ROOT/'tools/serve_pressure/verify.py', Path(__file__).resolve())}))
    print((OUTPUT/'tests.log').read_text())
    raise SystemExit(0 if result.wasSuccessful() else 1)
