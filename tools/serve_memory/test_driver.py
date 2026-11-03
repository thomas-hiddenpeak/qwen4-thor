"""No sockets/models: acceptance rejects missing or contradictory evidence."""
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_memory import stage, verify
from tools.serve_memory.cpu import CPU, save


def rows(opened):
    floor = 2048 if opened else 1048576
    state = "open" if opened else "memory_pressure"
    result = {verify.rejection_key(p, r): 0 for p in verify.POINTS for r in verify.REASONS}
    result.update({f'q4t_memory_admission_state{{state="{s}"}}': int(s == state)
                   for s in verify.STATES})
    result.update(q4t_memory_observation_valid=1, q4t_memory_admission_enabled=1,
        q4t_memory_observations_total=1, q4t_memory_observation_failures_total=0,
        q4t_memory_admission_floor_bytes=floor*1024**2,
        q4t_memory_admission_recovery_bytes=(floor+512)*1024**2,
        q4t_memory_admission_open=int(opened), q4t_ready=int(opened),
        q4t_memory_available_bytes=40*1024**3)
    return result, floor


class Driver(unittest.TestCase):
    def test_valid_open_and_closed_snapshots(self):
        for opened in (True, False):
            sample, floor = rows(opened)
            verify.memory_contract(sample, opened=opened, floor=floor)

    def test_invalid_sample_cannot_be_presented_as_healthy(self):
        for key in ("q4t_memory_observation_valid", "q4t_memory_observations_total",
                    "q4t_memory_available_bytes", "q4t_ready"):
            sample, floor = rows(True)
            sample[key] = 0
            with self.assertRaises((RuntimeError, AssertionError)):
                verify.memory_contract(sample, opened=True, floor=floor)

    def test_missing_or_extra_reason_is_rejected(self):
        original, floor = rows(False)
        for extra in (False, True):
            sample = copy.deepcopy(original)
            if extra:
                sample[verify.rejection_key("user_id_123", "memory_pressure")] = 1
            else:
                sample.pop(verify.rejection_key("execute", "memory_pressure"))
            with self.assertRaises((RuntimeError, AssertionError)):
                verify.memory_contract(sample, opened=False, floor=floor)

    def test_state_threshold_and_ready_must_agree(self):
        for key, value in (("q4t_ready", 1), ("q4t_memory_admission_open", 1),
                           ("q4t_memory_available_bytes", 2*1024**4),
                           ("q4t_memory_admission_recovery_bytes", 0)):
            sample, floor = rows(False)
            sample[key] = value
            with self.assertRaises((RuntimeError, AssertionError)):
                verify.memory_contract(sample, opened=False, floor=floor)

    def test_each_request_budget_refuses_before_increment(self):
        for kind in ("starts", "negatives", "generations"):
            run = verify.Run.__new__(verify.Run)
            run.deadline = 100
            run.counts = dict(stage.LIMITS)
            with patch.object(verify.time, "time", return_value=1):
                with self.assertRaises((RuntimeError, AssertionError)):
                    run.budget(kind)
            self.assertEqual(run.counts[kind], stage.LIMITS[kind])

    def test_elapsed_deadline_never_resets(self):
        run = verify.Run.__new__(verify.Run)
        run.deadline = 100
        run.counts = dict(generations=0, starts=0, negatives=0)
        with patch.object(verify.time, "time", return_value=100):
            with self.assertRaises((RuntimeError, AssertionError)):
                run.budget("generations")
        self.assertEqual(run.counts["generations"], 0)

    def test_http_preflight_response_schema_is_loaded_explicitly(self):
        # Request validators intentionally do not contain a response alias.
        self.assertEqual(set(verify.base.load_validators()), {"chat", "cancel", "preflight"})
        schema = json.loads((ROOT / "docs/serve-preflight-v1.schema.json").read_text())
        verify.Draft202012Validator.check_schema(schema)


if __name__ == "__main__":
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(Driver))
    with (CPU / "driver-tests.log").open("x") as output:
        output.write(stream.getvalue())
    save(CPU / "driver-result.json", dict(passed=result.wasSuccessful(), tests=result.testsRun))
    print(stream.getvalue())
    raise SystemExit(not result.wasSuccessful())
