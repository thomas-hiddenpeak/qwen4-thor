"""Fault isolation contracts, independent of CUDA, NVML and service processes."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_recovery import policy as p


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.context = p.Context(p.Desired.RUNNING, 2, 7)

    def fault(self, kind, **changes):
        return replace(p.Fault(kind, 2, 7), **changes)

    def test_optional_failures_do_not_require_gpu_visibility(self):
        for kind in p.AUXILIARY:
            with self.subTest(kind=kind):
                self.assertEqual(p.decide(self.fault(kind), self.context).action,
                                 p.Action.KEEP_SERVING)

    def test_explicit_device_failure_beats_timeout_and_missing_logs(self):
        result = p.decisions([self.fault(p.FaultKind.NVML_UNAVAILABLE),
            self.fault(p.FaultKind.DEVICE_FAILURE),
            self.fault(p.FaultKind.LOG_UNAVAILABLE)], self.context)
        self.assertEqual([item.action for item in result], [p.Action.QUARANTINE])

    def test_stop_wins_over_recovery_and_device_error(self):
        for desired in (p.Desired.STOPPED, p.Desired.MAINTENANCE):
            for kind in p.FaultKind:
                with self.subTest(desired=desired, kind=kind):
                    self.assertEqual(p.decide(self.fault(kind),
                        replace(self.context, desired=desired)).action,
                        p.Action.STOP)

    def test_old_and_future_generation_cannot_mutate_live_service(self):
        for controller, engine in ((1, 7), (2, 6), (3, 7), (2, 8)):
            for kind in p.FaultKind:
                with self.subTest(controller=controller, engine=engine,
                                  kind=kind):
                    result = p.decide(p.Fault(kind, controller, engine),
                                      self.context)
                    self.assertEqual(result.action, p.Action.IGNORE)

    def test_each_missing_safety_proof_blocks_replacement(self):
        ready = p.Evidence(True, True, True, True, True, True)
        for field in ('cleanup_complete', 'resources_safe', 'identity_valid',
                      'lease_continuous', 'device_usable'):
            for kind in (*p.MODEL_RECOVERABLE, p.FaultKind.CONTROLLER_EXIT):
                with self.subTest(field=field, kind=kind):
                    context = replace(self.context,
                        evidence=replace(ready, **{field: False}))
                    self.assertEqual(p.decide(self.fault(kind), context).action,
                                     p.Action.WAIT_DEPENDENCY)

    def test_valid_cleanup_and_independent_device_proof_allow_replacement(self):
        context = replace(self.context,
                          evidence=p.Evidence(True, True, True, True, True))
        # This proof may come from an allowed CUDA probe, not necessarily NVML.
        self.assertEqual(p.decide(self.fault(p.FaultKind.MODEL_EXIT),
                                 context).action, p.Action.RESTART_MODEL)

    def test_ple_and_oom_wait_for_dependency_recovery(self):
        for kind in (p.FaultKind.PLE_UNAVAILABLE, p.FaultKind.RESOURCE_EXHAUSTED):
            context = replace(self.context,
                evidence=p.Evidence(True, True, True, True, True, False))
            self.assertEqual(p.decide(self.fault(kind), context).action,
                             p.Action.WAIT_DEPENDENCY)

    def test_critical_resource_unknown_closes_admission(self):
        self.assertEqual(p.decide(self.fault(p.FaultKind.RESOURCE_UNKNOWN),
            self.context).action, p.Action.CLOSE_ADMISSION)

    def test_journal_unavailable_freezes_recovery_only(self):
        self.assertEqual(p.decide(self.fault(p.FaultKind.LEDGER_UNAVAILABLE),
            self.context).action, p.Action.FREEZE_RECOVERY)

    def test_capability_fault_does_not_become_unconstrained_success(self):
        for kind in (p.FaultKind.GRAMMAR_UNAVAILABLE,
                     p.FaultKind.MEDIA_UNAVAILABLE):
            self.assertEqual(p.decide(self.fault(kind), self.context).action,
                             p.Action.DISABLE_CAPABILITY)

    def test_request_failures_and_suspected_stall_do_not_restart_model(self):
        for kind, action in ((p.FaultKind.REQUEST, p.Action.REQUEST_ONLY),
                             (p.FaultKind.CACHE_INVALID, p.Action.REQUEST_ONLY),
                             (p.FaultKind.UNCONFIRMED_STALL,
                              p.Action.KEEP_SERVING)):
            self.assertEqual(p.decide(self.fault(kind), self.context).action,
                             action)

    def test_gateway_recovery_does_not_require_model_or_gpu_restart(self):
        context = replace(self.context,
            evidence=p.Evidence(cleanup_complete=True, identity_valid=True))
        self.assertEqual(p.decide(self.fault(p.FaultKind.GATEWAY_EXIT),
            context).action, p.Action.RESTART_GATEWAY)


if __name__ == '__main__':
    unittest.main()
