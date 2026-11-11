"""Validate cold business driver routing before any host maintenance or GPU."""
import os
import fcntl
import ctypes
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_recovery import cold_acceptance as driver, ledger, lifecycle, usage_store


class DriverTests(unittest.TestCase):
    def test_business_entry_retains_and_restores_real_subreaper(self):
        libc = ctypes.CDLL(None, use_errno=True)
        def value():
            result = ctypes.c_int()
            self.assertEqual(libc.prctl(37, ctypes.byref(result), 0, 0, 0), 0)
            return result.value
        before = value()
        def run():
            self.assertEqual(value(), 1)
            return 23
        with tempfile.TemporaryDirectory(dir=driver.ROOT/'.q4t-work') as temporary:
            plan = Path(temporary)/'plan.json'; plan.write_text('{}')
            with mock.patch.object(sys, 'argv', ['cold_acceptance.py','run','--plan',str(plan)]), \
                 mock.patch.object(driver, 'Stage') as stage:
                stage.return_value.run.side_effect = run
                self.assertEqual(driver.main(), 23)
        self.assertEqual(value(), before)

    def test_driver_hands_admission_only_to_its_own_launcher(self):
        actor = driver.Stage.__new__(driver.Stage)
        launch = [sys.executable, '-I', '-B', driver.LEGACY['__file__'], 'launcher', '--plan', '/plan']
        def run(_actor):
            driver.subprocess.Popen(launch, pass_fds=(10,), env={})
            driver.subprocess.Popen(['/bin/true'], pass_fds=())
            return 19
        with mock.patch.object(driver, 'descriptors', return_value=(21,22,23,24)), \
             mock.patch.object(driver.LEGACY['Stage'], 'run', run), \
             mock.patch.object(driver.subprocess, 'Popen') as called:
            self.assertEqual(actor.run(), 19)
        first, second = called.call_args_list
        self.assertEqual(first.args[0][3], str(Path(driver.__file__).resolve()))
        self.assertEqual(first.kwargs['pass_fds'], (10,21,22,23,24))
        self.assertEqual(second.kwargs['pass_fds'], ())

    def test_preinitialized_identity_and_quota_state_share_the_exact_domain_binding(self):
        actor = driver.Stage.__new__(driver.Stage)
        actor.plan = dict(output='/private/stage', service_id='service',
                          package=dict(manifest_sha256='a'*64))
        actor.package = Path('/private/package')
        actor.service = SimpleNamespace(descriptor=mock.Mock(return_value=dict(service_id='service')))
        actor.engine = SimpleNamespace(require=ledger.require,
            read=mock.Mock(return_value=dict(state=dict(desired='STOPPED'))))
        result = actor.initialize_state(); actor.service_id = result['service_id']
        with mock.patch.object(lifecycle, 'read_family', return_value={'known':'state'}) as read:
            self.assertEqual(actor.family('usage'), {'known':'state'})
        read.assert_called_once_with(Path('/private/stage/state'), dict(service_id='service'), usage_store.Store)
        actor.service.descriptor.return_value = dict(service_id='foreign')
        with self.assertRaises(ValueError): actor.initialize_state()

    def test_descriptor_parser_never_creates_or_consumes_a_capability(self):
        a, b = os.pipe(); c, d = os.pipe()
        try:
            os.write(b, b'live')
            with mock.patch.dict(os.environ, Q4T_DEVICE_ADMISSION_FDS=','.join(map(str,(a,b,c,d)))):
                self.assertEqual(driver.descriptors(), (a,b,c,d))
            self.assertEqual(os.read(a, 4), b'live')
            for value in ('1,2,3,4', '3,3,4,5', '3,4,5', '999999,999998,999997,999996'):
                with mock.patch.dict(os.environ, Q4T_DEVICE_ADMISSION_FDS=value), \
                     self.assertRaises((ValueError,OSError)): driver.descriptors()
        finally:
            for fd in (a,b,c,d): os.close(fd)

    def test_strict_guard_preserves_real_lease_and_only_explicit_admission(self):
        with tempfile.TemporaryDirectory(dir=driver.ROOT/'.q4t-work') as temporary:
            lock = Path(temporary)/'lease'; lock.write_text('')
            with lock.open('r+') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                plan = dict(lease_path=str(lock), output=temporary)
                with mock.patch.dict(os.environ, Q4T_MANAGED_LEASE_FD=str(held.fileno())), \
                     mock.patch.dict(driver.LEGACY, verify=lambda _:None), \
                     mock.patch.object(driver, 'descriptors', return_value=(21,22,23,24)), \
                     mock.patch.object(driver.os, 'execve') as execute:
                    driver.guard(plan, Path(temporary)/'plan.json')
                argv = execute.call_args.args[1]
                self.assertEqual(argv[argv.index('--compute-backend')+1], 'nvml')
                self.assertEqual(argv[argv.index('--lease-fd')+1], str(held.fileno()))
                self.assertEqual([argv[i+1] for i,v in enumerate(argv) if v == '--inherit-fd'],
                                 ['21','22','23','24'])
                self.assertIn('run', argv[argv.index('--')+1:])

    def test_strict_guard_rejects_unheld_lease_before_exec(self):
        with tempfile.TemporaryDirectory(dir=driver.ROOT/'.q4t-work') as temporary:
            lock = Path(temporary)/'lease'; lock.write_text('')
            with lock.open('r+') as held, \
                 mock.patch.dict(os.environ, Q4T_MANAGED_LEASE_FD=str(held.fileno())), \
                 mock.patch.dict(driver.LEGACY, verify=lambda _:None), \
                 mock.patch.object(driver, 'descriptors', return_value=(21,22,23,24)), \
                 mock.patch.object(driver.os, 'execve') as execute:
                with self.assertRaises(ValueError):
                    driver.guard(dict(lease_path=str(lock)), Path('/plan'))
                execute.assert_not_called()


if __name__ == '__main__': unittest.main()
