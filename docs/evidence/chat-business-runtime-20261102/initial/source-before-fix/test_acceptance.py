"""Offline identity and combined-result contracts; no process/model launches."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import acceptance


class AcceptanceContracts(unittest.TestCase):
    def test_interruption_drains_owned_guard_and_retains_failure(self):
        root = Path(os.environ.get("Q4T_TEST_OUTPUT_DIR",
                                   acceptance.ROOT / "build/acceptance-contracts"))
        root.mkdir(parents=True, exist_ok=True)
        for signum in (acceptance.signal.SIGTERM, acceptance.signal.SIGINT):
            with self.subTest(signal=signum), \
                 tempfile.TemporaryDirectory(dir=root) as temporary:
                output = Path(temporary) / "interrupted"
                plan = dict(artifacts={"python": dict(path="/fixture/python")})
                handlers = {}
                guard = Mock()
                guard.poll.return_value = None

                def wait_for_guard(timeout):
                    # First delivery unwinds execute; a repeated delivery during
                    # its cleanup must not abandon the same guard's bounded wait.
                    handlers[signum](signum, None)
                    return -int(signum)

                guard.wait.side_effect = wait_for_guard
                with patch.object(acceptance.signal, "signal",
                                  side_effect=handlers.__setitem__), \
                     patch.object(acceptance, "validate_plan"), \
                     patch.object(acceptance.subprocess, "Popen",
                                  return_value=guard) as launch:
                    acceptance.install_interrupt_handlers()
                    self.assertEqual(set(handlers),
                        {acceptance.signal.SIGINT, acceptance.signal.SIGTERM})
                    with self.assertRaises(KeyboardInterrupt):
                        acceptance.execute(plan, output)
                launch.assert_called_once()
                guard.terminate.assert_called_once_with()
                self.assertEqual(guard.wait.call_args_list, [
                    call(timeout=acceptance.LIMITS['stage_seconds'] + 120),
                    call(timeout=90)])
                result = json.loads((output / "result.json").read_text())
                self.assertEqual(result["status"], "FAIL")
                self.assertIn(acceptance.signal.Signals(signum).name,
                              result["error"])
                self.assertFalse(result["deployed"])
                saved_plan = json.loads((output / "plan.json").read_text())
                self.assertEqual(saved_plan, plan)
                self.assertTrue((output / "guard-command.json").is_file())
                self.assertTrue((output / "guard.log").is_file())

    def test_worker_success_does_not_override_guard_failure(self):
        worker = dict(status="CHECKS_PASSED_PENDING_GUARD_EXIT")
        guard = dict(status="COMPLETED", child_returncode=0, finished="stamp")
        self.assertEqual(acceptance.combined_status(worker, guard, 0), "PASS")
        for state in ("ENV_ABORT", "LEASE_BUSY", "RECOVERY_REQUIRED"):
            self.assertEqual(acceptance.combined_status(worker,
                dict(guard, status=state), 0), "ENV_ABORT")
        self.assertEqual(acceptance.combined_status(worker,
            dict(guard, cleanup_error="unreaped child"), 0), "ENV_ABORT")
        for incomplete in ({}, dict(status="COMPLETED"),
                           dict(guard, child_returncode=1)):
            self.assertEqual(acceptance.combined_status(worker, incomplete, 0), "FAIL")
        self.assertEqual(acceptance.combined_status({}, guard, 0), "FAIL")
        self.assertEqual(acceptance.combined_status(
            dict(status="ENV_ABORT"), guard, 1), "ENV_ABORT")
        self.assertEqual(acceptance.combined_status(worker, guard, 1), "FAIL")

    def test_identity_changes_stop_before_launch(self):
        root = Path(os.environ.get("Q4T_TEST_OUTPUT_DIR",
                                   acceptance.ROOT / "build/acceptance-contracts"))
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            directory = Path(temporary)
            source = directory / "input.txt"
            source.write_text("source")
            source_hash = acceptance.digest(source)
            protocol_hash = acceptance.hashlib.sha256(
                f'input.txt:{source_hash}\n'.encode()).hexdigest()
            header = directory / "identity.h"
            header.write_text('kProtocol[] = "' + protocol_hash + '";')
            manifest = directory / "identity.json"
            manifest.write_text(json.dumps({"input.txt": source_hash}))
            plan = dict(schema_version=1, root=str(directory), limits=acceptance.LIMITS,
                port=18096, artifacts={key: acceptance.artifact(header) for key in
                ("candidate", "baseline", "host_tests", "template_checker", "python",
                 "identity_header")}, protocol_sha256=protocol_hash,
                sources={"input.txt": acceptance.digest(source)},
                model_dir=str(directory), model_inputs={"input.txt": acceptance.digest(source)})
            plan['artifacts']['identity_sources'] = acceptance.artifact(manifest)
            with patch.object(acceptance, "ROOT", directory), \
                 patch.object(acceptance, "source_paths", return_value=["input.txt"]), \
                 patch.object(acceptance.subprocess, "Popen",
                              side_effect=AssertionError("launch forbidden")):
                acceptance.validate_plan(plan)
                changed = deepcopy(plan)
                changed["limits"]["generations"] = 25
                with self.assertRaises(RuntimeError):
                    acceptance.validate_plan(changed)
                source.write_text("changed")
                with self.assertRaises(RuntimeError):
                    acceptance.validate_plan(plan)
                source.write_text("source")
                header.write_text("changed artifact")
                with self.assertRaises(RuntimeError):
                    acceptance.validate_plan(plan)

    def test_commands_keep_one_http_driver_and_fixed_bounds(self):
        plan = dict(artifacts={key: dict(path="/fixture/" + key) for key in
            ("candidate", "baseline", "host_tests", "template_checker", "python")},
            model_dir="/fixture/model", port=18096, protocol_sha256="a" * 64)
        steps = acceptance.commands(plan, Path("/fixture/output"))
        self.assertEqual([s[0] for s in steps],
            ["host", "client", "recovery", "orchestration", "template",
             "faults", "http"])
        http = steps[-1][2]
        self.assertEqual(http.count("--candidate"), 1)
        self.assertEqual(http[http.index("--protocol-sha256") + 1], "a" * 64)
        self.assertTrue(all(0 < seconds <= 1860 for _, seconds, _ in steps))
        self.assertEqual(acceptance.LIMITS["planned_generations"], 23)
        faults = steps[-2]
        self.assertEqual(faults[1], 210)
        self.assertEqual(faults[2][-2:], ["--output", "/fixture/output/faults"])
        self.assertNotIn("--candidate", faults[2])


if __name__ == "__main__":
    unittest.main()
