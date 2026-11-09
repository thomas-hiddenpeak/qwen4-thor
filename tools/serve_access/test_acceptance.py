"""N04 driver boundaries only; no model, TLS listener or GPU operations."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_access import acceptance as a


def require(value, message):
    if not value:raise ValueError(message)


class DriverTests(unittest.TestCase):
    def stage(self):
        root = a.ROOT/'.q4t-work/serve-access-20261109/driver-fixtures';root.mkdir(parents=True,exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=root);self.addCleanup(directory.cleanup)
        value = a.Stage.__new__(a.Stage)
        value.output = Path(directory.name);value.scope = value.output/'state';value.scope.mkdir()
        value.saved_files = [];value.engine = SimpleNamespace(require=require)
        return value

    def test_pending_gate_requires_exact_identity_and_keeps_production_verifier(self):
        metadata = dict(validation_only=True,acceptance='VALIDATION_ONLY_N04',binary_sha256='a'*64)
        decision = dict(status='PENDING_REAL_BUSINESS_N04',observed=False)
        original = mock.Mock(side_effect=ValueError('production rejected'))
        gate = a.validation_gate(original,dict(metadata=a.fingerprint(metadata),decision=a.fingerprint(decision)))
        gate(metadata,decision);original.assert_not_called()
        for m,d in ((dict(metadata,binary_sha256='b'*64),decision),
                    (metadata,dict(decision,observed=True))):
            with self.assertRaises(ValueError):gate(m,d)
        with self.assertRaisesRegex(ValueError,'production rejected'):gate(dict(acceptance='anything'),decision)
        original.assert_called_once()

    def test_fault_restore_never_overwrites_a_new_or_pending_commit(self):
        stage = self.stage();path = stage.scope/'client-usage';path.mkdir()
        (path/'policy.json').write_bytes(b'old active')
        pair = stage.unavailable_file('usage')
        (path/'policy.pending').write_bytes(b'new pending')
        with self.assertRaises(ValueError):stage.restore_file(pair)
        self.assertEqual(pair[1].read_bytes(),b'old active')
        (path/'policy.pending').unlink();(path/'policy.json').write_bytes(b'new active')
        with self.assertRaises(ValueError):stage.restore_file(pair)
        self.assertEqual((path/'policy.json').read_bytes(),b'new active')
        (path/'policy.json').unlink();stage.restore_file(pair)
        self.assertFalse(stage.saved_files);self.assertEqual((path/'policy.json').read_bytes(),b'old active')

    def test_failed_core_cleanup_does_not_restore_faulted_files_under_live_writers(self):
        stage = self.stage();stage.saved_files = [('a','b')];stage.restore_file = mock.Mock()
        with mock.patch.object(a.common.Stage,'stop',side_effect=ValueError('cleanup unknown')):
            with self.assertRaisesRegex(ValueError,'cleanup unknown'):stage.stop()
        stage.restore_file.assert_not_called()
        with mock.patch.object(a.common.Stage,'stop',return_value=None):stage.stop()
        stage.restore_file.assert_called_once_with(('a','b'))

    def test_unknown_ready_deadline_cannot_be_hidden_by_later_management_latency(self):
        stage = self.stage();clock = [100.0]
        stage.quota_deadlines = {3:150.0};stage.wait = mock.Mock(return_value=dict(business_ready=True))
        def status(*_args):clock[0]=200.0;return dict(continuity='conservative_new_window')
        stage.management = status
        with mock.patch.object(a.time,'monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(ValueError,'complete window'):stage.ready(3,mode='conservative_new_window')
        clock[0]=151.0
        with mock.patch.object(a.time,'monotonic',side_effect=lambda:clock[0]):
            self.assertTrue(stage.ready(3,mode='conservative_new_window')['business_ready'])
        stage.management = lambda *_args:dict(continuity='exact');clock[0]=100.0
        with mock.patch.object(a.time,'monotonic',side_effect=lambda:clock[0]):
            self.assertTrue(stage.ready(2,mode='exact')['business_ready'])


if __name__ == '__main__':unittest.main()
