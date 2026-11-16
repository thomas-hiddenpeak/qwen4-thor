"""Checkpoint and registration retirement is reversible across partial renames."""
import copy
import os
from pathlib import Path
import unittest
from unittest import mock

from tools.serve_recovery import root_checkpoints, root_transfer, test_version_archive
from tools.serve_recovery import version_archive as archive
from tools.serve_recovery import version_registration


class CheckpointArchiveTests(unittest.TestCase):
    def setUp(self):
        self.base = test_version_archive.ArchiveTests()
        self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.state, self.cycle = self.base.state, self.base.retired
        self.binding = self.base.base.binding
        self.marker = root_checkpoints.identity(self.binding,
            archive.engine.sha(self.base.source), self.cycle)
        root_checkpoints.write(self.state, self.marker, self.base.spent)
        self.pool = self.state/root_checkpoints.DIRECTORY

    def test_registration_and_last_consumed_state_archive_and_restore_together(self):
        plan = self.base.prepare()
        original = {p.name:p.read_bytes() for p in self.pool.iterdir()}
        current = self.base.base.load()
        archive.apply(plan['plan'], plan['sha256'])
        self.assertEqual(list(self.pool.iterdir()), [])
        self.assertFalse(self.base.source.exists())
        archive.apply(plan['plan'], plan['sha256'], restore=True)
        self.assertEqual({p.name:p.read_bytes() for p in self.pool.iterdir()}, original)
        self.assertEqual(root_checkpoints.read(self.state, self.marker), self.base.spent)
        self.assertEqual(self.base.base.load(), current)

    def test_interrupted_marker_and_snapshot_movement_resumes_same_plan(self):
        plan, original = self.base.prepare(), os.rename
        def fail(src, dst):
            if Path(src).parent == self.pool and str(src).endswith('.json'):
                raise OSError('CPU archive checkpoint interruption')
            return original(src, dst)
        with mock.patch.object(os, 'rename', side_effect=fail), self.assertRaises(OSError):
            archive.apply(plan['plan'], plan['sha256'])
        self.assertFalse(self.base.source.exists())
        self.assertFalse((self.pool/(self.cycle+'.bound')).exists())
        archive.apply(plan['plan'], plan['sha256'])
        archive.apply(plan['plan'], plan['sha256'], restore=True)
        self.assertEqual(root_checkpoints.read(self.state, self.marker), self.base.spent)

    def test_changed_missing_or_partial_checkpoint_does_not_reuse_older_seed(self):
        plan = self.base.prepare()
        snapshot = self.pool/(self.cycle+'.json')
        snapshot.write_bytes(snapshot.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError, 'checkpoint bytes'):
            archive.apply(plan['plan'], plan['sha256'])
        self.assertTrue(self.base.source.exists())
        snapshot.rename(self.state/'missing-snapshot.saved')
        with self.assertRaisesRegex(ValueError, 'missing or needs'):
            root_checkpoints.read(self.state, self.marker)
        with mock.patch.object(version_registration, 'MAX_CYCLES', 0), \
                self.assertRaisesRegex(ValueError, 'bound'):
            root_checkpoints.directory(self.state)

    def test_latest_transfer_protection_and_unrelated_state_cannot_replenish_history(self):
        selected = self.base.base.load()
        source = self.base.desc
        import uuid
        request = str(uuid.uuid4())
        target = dict(version=2, service_id=source['service_id'],
            **self.base.base.root, deployment_revision=source.get('deployment_revision', 0)+1,
            deployment_id=request)
        intent = root_transfer.deployment_policy.prepare(
            root_transfer.deployment_policy.initial(source), target, request, [])
        rows = root_transfer.records(self.binding)
        value = root_transfer.prepare(self.binding, intent, selected, rows)
        root_transfer.save(self.binding, rows, value)
        with self.assertRaisesRegex(ValueError, 'retained root'):
            self.base.prepare()
        erased = copy.deepcopy(self.base.spent)
        erased.update(switch=None, faults=[], revision=erased['revision']+1)
        with self.assertRaisesRegex(ValueError, 'refund'):
            root_checkpoints.write(self.state, self.marker, erased)
        self.assertEqual(root_checkpoints.read(self.state, self.marker), self.base.spent)


if __name__ == '__main__': unittest.main()
