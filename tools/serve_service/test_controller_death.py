"""Real CPU ownership/lease tests and interrupted-response failure gates."""
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tools.serve_service import acceptance as a, package as pkg, supervisor as sup
from tools.serve_service import controller_death as phase
from tools.serve_service.lease_custody import (
    Custody, EnvironmentAbort, copy_owned_lease)
from jsonschema import Draft202012Validator


class Tests(unittest.TestCase):
    def setUp(self):
        parent = phase.ROOT/'build/controller-death-tests'
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.child = None
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.child:
            if self.child.poll() is None:
                self.child.kill()
            self.child.wait(3)
            self.child.stdout.close()

    def controller(self, shared=False):
        self.lock = self.root/'lease.lock'
        self.child = subprocess.Popen([sys.executable, '-B', '-c',
            'import fcntl,sys,time; s=open(sys.argv[1],"a+"); '
            f'fcntl.flock(s,fcntl.LOCK_{"SH" if shared else "EX"}); '
            'print(s.fileno(),flush=True); time.sleep(30)', str(self.lock)],
            stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.assertTrue(self.child.stdout.readline().strip().isdigit())
        return sup.process_identity(self.child.pid)

    def test_original_description_stays_exclusive_after_original_owner_dies(self):
        owner = self.controller()
        fd, proof = copy_owned_lease(owner, self.lock)
        try:
            self.assertEqual(proof['inode'], self.lock.stat().st_ino)
            self.child.kill(); self.child.wait(3)
            self.assertFalse(a.lock_available(self.lock))
        finally:
            os.close(fd)
        self.assertTrue(a.lock_available(self.lock))

    def test_recycled_identity_or_non_child_cannot_export_a_lease(self):
        owner = self.controller()
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            copy_owned_lease(dict(owner, start_ticks=owner['start_ticks']+1), self.lock)
        with self.assertRaisesRegex(RuntimeError, 'not our direct child'):
            copy_owned_lease(sup.process_identity(os.getpid()), self.lock)
        self.assertIsNone(self.child.poll())

    def test_different_inode_and_shared_locks_are_not_exclusive_custody(self):
        owner = self.controller(shared=True)
        other = self.root/'other.lock'; other.touch()
        with self.assertRaisesRegex(RuntimeError, 'no repository lease'):
            copy_owned_lease(owner, other)
        with self.assertRaisesRegex(ValueError, 'exclusively locked'):
            copy_owned_lease(owner, self.lock)

    def custody(self, scan):
        owner = self.controller()
        out = self.root/'custody'; out.mkdir()
        self.failed = threading.Event()
        return Custody(owner, self.lock, out, scan=scan, interval=.01,
                       on_failure=self.failed.set)

    def test_monitor_failure_cannot_become_completed_cleanup(self):
        n = 0
        def scan():
            nonlocal n
            n += 1
            if n > 1: raise OSError('fixed NVML failure')
            return {'conflicts':[]}
        custody = self.custody(scan)
        custody.start()
        self.assertTrue(self.failed.wait(2))
        self.child.kill(); self.child.wait(3)
        with self.assertRaises(EnvironmentAbort):
            custody.finish(cleanup_confirmed=True)
        self.assertEqual(pkg.read(self.root/'custody/exit.json')['status'], 'ENV_ABORT')
        self.assertTrue(a.lock_available(self.lock))

    def test_competing_workload_fails_before_independent_monitor_starts(self):
        custody = self.custody(lambda: {'conflicts':[{'pid':123}]})
        with self.assertRaises(EnvironmentAbort): custody.start()
        self.assertIsNone(self.child.poll())  # Never signal a conflict/foreign PID.
        self.child.kill(); self.child.wait(3)
        with self.assertRaises(EnvironmentAbort): custody.finish(cleanup_confirmed=True)

    def test_cleanup_must_be_proven_before_successful_custody_terminal(self):
        custody = self.custody(lambda: {'conflicts':[]})
        custody.start()
        self.child.kill(); self.child.wait(3)
        with self.assertRaisesRegex(RuntimeError, 'cleanup not confirmed'):
            custody.finish(cleanup_confirmed=False)
        self.assertEqual(pkg.read(self.root/'custody/exit.json')['status'], 'ENV_ABORT')

    def test_dead_controller_cannot_claim_a_recycled_process_group(self):
        custody = self.custody(lambda: {'conflicts':[]})
        self.child.kill(); self.child.wait(3)
        with mock.patch.object(a.guard, 'inspect', return_value={'conflicts':[]}) as inspect:
            custody.inspect()
            self.assertEqual(inspect.call_args.args[0], -1)
        custody.finish(cleanup_confirmed=True)
        self.assertEqual(pkg.read(self.root/'custody/exit.json')['status'], 'COMPLETED')

    def test_abrupt_controller_exit_requires_complete_backend_shutdown_protocol(self):
        chunk = dict(id='fixture', object='chat.completion.chunk', created=1, model='m',
            choices=[dict(index=0, delta=dict(role='assistant', content='1'),
                          finish_reason=None, logprobs=None)])
        error = dict(error=dict(message='stopping', type='server_error',
                               param=None, code='server_shutdown'))
        raw = b''.join(b'data: '+json.dumps(x).encode()+b'\n\n' for x in (chunk,error))
        raw += b'data: [DONE]\n\n'
        validator = Draft202012Validator(pkg.read(phase.ROOT/'docs/serve-transport-v1.schema.json'))
        value = phase.stream_terminal(raw, graceful=True, validator=validator)
        self.assertFalse(value['success'])
        for damaged in (raw.replace(b'data: [DONE]\n\n',b''),
                        raw.replace(b'server_shutdown',b'service_unavailable'),
                        raw.replace(b'"finish_reason": null',b'"finish_reason": "stop"')):
            with self.assertRaises(RuntimeError):
                phase.stream_terminal(damaged, graceful=True, validator=validator)


if __name__ == '__main__':
    unittest.main()
