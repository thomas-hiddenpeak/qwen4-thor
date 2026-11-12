"""Actual CPU child/lease lifecycle; host GPU policy is deliberately mocked."""
import fcntl
import json
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


@unittest.skipUnless(os.geteuid()==0,'explicit root CPU source verification')
class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.uid,self.gid=int(os.environ['SUDO_UID']),int(os.environ['SUDO_GID'])
        self.temp=tempfile.TemporaryDirectory(dir=host.ROOT/'.q4t-work');self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name);os.chown(self.path,self.uid,self.gid)
        self.plan=dict(runtime_uid=self.uid,runtime_gid=self.gid,output=str(self.path),
            package=dict(package=str(self.path)))
        (self.path/'manifest.json').write_text('{"cpu_fixture":true}')
        self.private=self.path/'private.json';self.private.write_bytes(b'CPU owner-only policy')
        os.chown(self.private,self.uid,self.gid);self.private.chmod(0o400)
        original=host.domain;self.addCleanup(setattr,host,'domain',original)

    def test_privileged_cold_preflight_checks_private_policy_as_original_owner(self):
        from tools.serve_gateway import client_policy
        from tools.serve_service import package
        def verify(_):
            assert os.getresuid()==(self.uid,)*3 and os.getresgid()==(self.gid,)*3
            assert os.getgroups()==[] and client_policy.read_private(self.private)==b'CPU owner-only policy'
            (self.path/'verified.json').write_text(json.dumps(dict(uid=os.geteuid())))
        with mock.patch.dict(host.business.LEGACY,verify=verify,
                load_modules=lambda _:(None,None,None,None,None,package)),\
             mock.patch.object(host,'execute',side_effect=AssertionError('no device action allowed')):
            self.assertEqual(host.load_checked(self.plan),dict(cpu_fixture=True))
        self.assertEqual(json.loads((self.path/'verified.json').read_text()),dict(uid=self.uid))
        self.assertEqual(os.geteuid(),0)

    def test_failed_private_verification_never_loads_privileged_adapter(self):
        def verify(_):raise ValueError('CPU private identity rejected')
        loader=mock.Mock(side_effect=AssertionError('adapter loaded after rejection'))
        with mock.patch.dict(host.business.LEGACY,verify=verify,load_modules=loader),\
             self.assertRaisesRegex(ValueError,'verification failed'):
            host.load_checked(self.plan)
        loader.assert_not_called();self.assertEqual(os.geteuid(),0)


if __name__ == '__main__': unittest.main()
