"""Actual CPU child/lease lifecycle; host GPU policy is deliberately mocked."""
import fcntl
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_recovery import cold_host_acceptance as host


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=host.ROOT/'.q4t-work')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory/'lease'; self.path.write_text('')
        self.held = self.path.open('r+'); self.addCleanup(self.held.close)
        fcntl.flock(self.held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.disarmed = False

    def check_lease(self):
        with self.path.open('r+') as other:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def disarm(self, *_args):
        self.check_lease(); self.disarmed = True
        return dict(armed=False)

    def run_child(self, entry, *, arm=None, disarm=None, seconds=2):
        adapter = SimpleNamespace(
            arm=mock.Mock(side_effect=arm or (lambda *_:dict(armed=True))),
            enter=mock.Mock(side_effect=entry), disarm=mock.Mock(side_effect=disarm or self.disarm))
        with mock.patch.object(host, 'domain', adapter):
            return host.execute(self.directory, self.held.fileno(), self.path,
                                'a'*64, ['/bin/true'], seconds=seconds)

    def test_real_child_exit_keeps_parent_lease_through_disarm(self):
        def child(*_args):
            self.check_lease()
            (self.directory/'child-pid').write_text(str(os.getpid()))
            os._exit(0)
        result = self.run_child(child)
        self.assertEqual(result['status'], 'PASS'); self.assertTrue(self.disarmed)
        self.assertTrue(result['domain_removed'])
        self.assertFalse(Path('/proc/'+(self.directory/'child-pid').read_text()).exists())
        self.check_lease()

    def test_busy_qualification_never_forks_or_enters(self):
        with mock.patch.object(host.os, 'fork') as fork:
            result = self.run_child(lambda *_:self.fail('entry after BUSY'),
                                    arm=ValueError('BUSY'))
        self.assertEqual(result['status'], 'ENV_ABORT')
        fork.assert_not_called(); self.assertTrue(self.disarmed)

    def test_real_timed_out_child_is_reaped_before_disarm(self):
        def child(*_args):
            (self.directory/'child-pid').write_text(str(os.getpid()))
            time.sleep(10)
        result = self.run_child(child, seconds=.15)
        self.assertEqual(result['status'], 'ENV_ABORT')
        self.assertIsNotNone(result['child_exit']); self.assertTrue(self.disarmed)
        self.assertFalse(Path('/proc/'+(self.directory/'child-pid').read_text()).exists())

    def test_populated_domain_cleanup_never_reports_success(self):
        result = self.run_child(lambda *_:os._exit(0), disarm=ValueError('populated'))
        self.assertEqual(result['status'], 'ENV_ABORT')
        self.assertFalse(result['domain_removed']); self.check_lease()


if __name__ == '__main__': unittest.main()
