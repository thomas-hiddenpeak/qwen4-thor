"""Preserve established migration support; no package acceptance claim."""
import os
from pathlib import Path
import tempfile
import unittest

from tools.serve_recovery import deployment, root_checkpoints, root_transfer


class ContractTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR', Path.cwd()/'build/root-contract'))
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup);self.state = Path(temporary.name)
        self.old = dict(version=18, files={})
        self.new = dict(version=19, files={'source/tools/serve_recovery/'+n+'.py':'f'*64
            for n in ('root_checkpoints', 'root_transfer', 'root_version_policy')})

    def test_checkpoint_or_partial_intent_requires_complete_migration_support(self):
        for name in (root_checkpoints.DIRECTORY, root_transfer.INTENT,
                     root_transfer.INTENT+'.new'):
            marker = self.state/name;marker.touch()
            with self.assertRaisesRegex(ValueError, 'root recovery migration'):
                deployment.preserve_outer(self.state, self.old)
            deployment.preserve_outer(self.state, self.new)
            for missing in self.new['files']:
                broken = dict(self.new, files={n:h for n,h in self.new['files'].items() if n != missing})
                with self.assertRaisesRegex(ValueError, 'root recovery migration'):
                    deployment.preserve_outer(self.state, broken)
            self.assertTrue(marker.exists());marker.unlink()

    def test_unestablished_migration_does_not_invent_an_old_root_requirement(self):
        deployment.preserve_outer(self.state, self.old)
        with self.assertRaisesRegex(ValueError, 'root recovery migration'):
            deployment.preserve_outer(self.state, self.old, migration=True)
        deployment.preserve_outer(self.state, self.new, migration=True)
        self.assertEqual(list(self.state.iterdir()), [])


if __name__ == '__main__': unittest.main()
