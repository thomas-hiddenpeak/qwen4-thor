"""Cold hints cannot bypass the recovery root, actual probe or device faults."""
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_recovery import cold_admission, controller, policy


class ControllerTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2]
        base = root/'.q4t-work/serve-device-guardian-20261112/controller-tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        (self.path/'bin').mkdir()
        self.lease = os.open(self.path/'lease', os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        self.addCleanup(os.close, self.lease)
        fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        actor = controller.Controller.__new__(controller.Controller)
        self.actor = actor
        actor.tick = lambda **_kwargs: False
        actor.protect = lambda **_kwargs: False
        actor.ownership_unknown = False
        actor.monitor = SimpleNamespace(job=None, visibility='UNKNOWN')
        actor.profiles = None
        actor.base = dict(model_dir=str(self.path))
        actor.manifest = dict(model_assets={})
        actor.settings = {}
        actor.need_cuda_probe = actor.probe_passed = actor.continuous = False
        actor.cold_device_recovery = True
        actor.transaction = mock.Mock(return_value=dict(ok=False, result=dict(reason='probe_not_authorized')))
        actor.quarantine = mock.Mock()
        actor.last_fault = policy.FaultKind.MODEL_EXIT
        actor.channel = SimpleNamespace(generation=1)
        actor.package = self.path
        actor.lease_fd = self.lease
        stack = self.enterContext(ExitStack())
        stack.enter_context(mock.patch.object(controller.memory, 'read',
            return_value=dict(available_bytes=5*1024**3)))
        stack.enter_context(mock.patch.object(controller.bundle, 'check_assets'))
        stack.enter_context(mock.patch.object(controller.supervisor, 'preflight_ports'))

    def probe(self, *, code=0):
        value = dict(version=1, ok=code == 0, cuda_code=code, stage='complete')
        path = self.path/'bin/q4t-cuda-probe'
        path.write_text('#!'+sys.executable+'\nprint('+repr(json.dumps(value))+')\n')
        path.chmod(0o500)
        self.actor.transaction.return_value = dict(ok=True, result=dict(allowed=True, generation=1))

    def test_absent_cold_credential_preserves_wait_without_probe_or_grant(self):
        self.actor.cold_device_recovery = False
        with mock.patch.object(controller.jobs, 'Job') as start:
            self.assertIsNone(self.actor.preflight())
        self.assertEqual(self.actor.reason, 'cold_nvml_unavailable')
        self.actor.transaction.assert_not_called(); start.assert_not_called()

    def test_cold_hint_still_requires_root_permission_before_any_probe(self):
        with mock.patch.object(controller.jobs, 'Job') as start:
            self.assertIsNone(self.actor.preflight())
        self.assertEqual(self.actor.reason, 'probe_not_authorized')
        self.assertEqual(self.actor.transaction.call_args.args[0], 'probe')
        start.assert_not_called()

    def test_root_permission_runs_actual_cpu_probe_but_not_model_authorization(self):
        self.probe()
        proof = self.actor.preflight()
        self.assertTrue(proof['device_usable'])
        self.assertTrue(self.actor.probe_passed)
        self.assertIsNone(self.actor.probe_job)
        self.assertEqual(self.actor.preflight(), proof)
        self.assertEqual(self.actor.transaction.call_count, 1)
        self.assertEqual(self.actor.transaction.call_args.args[0], 'probe')
        self.actor.quarantine.assert_not_called()

    def test_true_device_failure_is_quarantined_after_authorized_probe(self):
        self.probe(code=214)
        self.assertIsNone(self.actor.preflight())
        self.actor.quarantine.assert_called_once_with('device_failure')
        self.assertFalse(self.actor.probe_passed)

    def test_unreaped_probe_retains_slot_and_does_not_prepare_model(self):
        self.actor.transaction.return_value = dict(ok=True, result=dict(allowed=True))
        pending = object()
        with mock.patch.object(controller.jobs, 'Job', return_value=pending), \
             mock.patch.object(controller.jobs, 'finish', return_value=None):
            self.assertIsNone(self.actor.preflight())
        self.assertIs(self.actor.probe_job, pending)
        self.actor.quarantine.assert_called_once_with('cleanup_failed')
        self.assertFalse(self.actor.probe_passed)

    def test_launcher_absence_and_invalid_descriptor_input_never_manufacture_authority(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cold_admission.configuration('service', 'a'*64, self.lease),
                             (None, ()))
        for raw in ('1,2,3,4', '3,3,4,5', '3,4,5', 'a,b,c,d', '9999999,4,5,6'):
            with self.subTest(raw=raw), mock.patch.dict(os.environ,
                    {cold_admission.ENVIRONMENT: raw}), self.assertRaises(ValueError):
                cold_admission.configuration('service', 'a'*64, self.lease)


if __name__ == '__main__': unittest.main()
