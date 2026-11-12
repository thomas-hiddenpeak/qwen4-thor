"""Actual private version journal failures; no model/service starts."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_store, version_policy, version_store
from tools.serve_recovery import test_version_policy as fixtures


class StoreTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[2]/'.q4t-work/serve-version-recovery-20261111/state-fixtures'
        root.mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir=root);self.addCleanup(temporary.cleanup)
        self.path=Path(temporary.name)
        self.fixture=fixtures.PolicyTests();self.fixture.setUp()
        self.binding=access_store.binding(self.path,str(uuid.uuid4()))
        version_store.initialize_bound(self.binding,self.fixture.state)
        self.request=dict(sequence=1,op='commit',request_id=self.fixture.request,
                          expected_revision=0,candidate=self.fixture.pending())

    def test_rename_failure_recovers_same_consumed_fallback_and_receipt(self):
        with mock.patch.object(os,'replace',side_effect=OSError('before rename')):
            with self.assertRaises(OSError):version_store.execute(self.binding,self.request)
        self.assertTrue((self.path/'version-selection/policy.pending').exists())
        value=version_store.execute(self.binding,self.request)
        self.assertEqual(value,self.request['candidate'])
        self.assertEqual(version_store.execute(self.binding,self.request),value)
        with version_store.open_bound(self.binding) as writer:
            self.assertEqual(len(writer.head['state']['receipts']),1)

    def test_postrename_fsync_unknown_and_cross_boot_do_not_create_new_cycle(self):
        with version_store.open_bound(self.binding) as writer:
            original=os.fsync
            def fail(fd):
                if fd==writer.dir_fd:raise OSError('directory fsync receipt lost')
                return original(fd)
            with mock.patch.object(os,'fsync',side_effect=fail),self.assertRaises(OSError):
                writer.transact(self.request['request_id'],0,self.request['candidate'])
        value=version_store.execute(self.binding,self.request)
        resumed=version_policy.resume(value,str(uuid.uuid4()))
        actual=version_store.execute(self.binding,dict(sequence=2,op='commit',
            request_id=str(uuid.uuid4()),expected_revision=1,candidate=resumed))
        self.assertEqual(actual['switch'],value['switch'])
        self.assertEqual(actual['registry'],value['registry'])
        self.assertTrue(version_policy.describe(actual)['fallback_consumed'])

    def test_missing_state_and_writer_conflict_never_bootstrap_a_free_fallback(self):
        version_store.execute(self.binding,self.request)
        with version_store.open_bound(self.binding):
            with self.assertRaises(BlockingIOError):version_store.execute(self.binding,self.request)
        shutil.rmtree(self.path/'version-selection')
        with self.assertRaises(OSError):
            version_store.initialize_bound(self.binding,self.fixture.state)
        self.assertTrue((self.path/'version-selection.json').exists())
        self.assertFalse((self.path/'version-selection').exists())


if __name__=='__main__':unittest.main()
