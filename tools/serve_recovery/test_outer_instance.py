"""Actual Guardian adoption keeps one instance fence through root retirement."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import outer_instance, test_runtime as fixture


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.output = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        self.output.mkdir(parents=True,exist_ok=True)
        self.owner = None
        self.raw_fd = None
        self.addCleanup(self.release)

    def release(self):
        if self.owner is not None:self.owner.close()
        if self.raw_fd is not None:os.close(self.raw_fd);self.raw_fd=None

    def service(self, *, kind='held', resume=False, daemon=False):
        original = subprocess.Popen
        def spawn(argv, **options):
            if argv[0] == str(fixture.BINARY):
                path = Path(argv[2]);config=json.loads(path.read_text())
                state=Path(config['directory'])
                if kind == 'held':
                    self.owner=outer_instance.Instance(state,config['service_id']).__enter__()
                    fd=self.owner.fd
                else:
                    expected=state/'guardian.lock';expected.touch(mode=0o600)
                    target=expected if kind=='unheld' else state/'wrong-instance.lock'
                    self.raw_fd=os.open(target,os.O_RDWR | os.O_CREAT,0o600);fd=self.raw_fd
                    if kind=='wrong':fcntl.flock(fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
                config.update(version=5,usage_handoff=False,version_recovery=None,
                              cold_admission=None,outer_instance_fd=fd)
                path.write_text(json.dumps(config));options['pass_fds']=(*options['pass_fds'],fd)
                if resume:argv=[*argv[:-1],'--resume']
            return original(argv,**options)
        with mock.patch.object(subprocess,'Popen',side_effect=spawn):
            value=fixture.Service(self.output,daemon=daemon)
        self.addCleanup(value.close)
        return value

    def unavailable(self, path):
        fd=os.open(path,os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:os.close(fd)

    def test_parent_keeps_actual_instance_after_guardian_stops(self):
        value=self.service();value.ready();self.owner.check()
        self.assertTrue(value.stop()['stop_durable'])
        self.owner.check();self.unavailable(value.directory/'guardian.lock')
        self.owner.close()
        fd=os.open(value.directory/'guardian.lock',os.O_RDWR)
        try:fcntl.flock(fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:os.close(fd)

    def test_parent_close_does_not_unlock_live_guardian_duplicate(self):
        value=self.service();value.ready();self.owner.close()
        self.unavailable(value.directory/'guardian.lock')
        self.assertTrue(value.stop()['cleanup_confirmed'])

    def test_resume_keeps_original_stopped_intent_without_role_launch(self):
        value=self.service(resume=True)
        status=value.wait(lambda row:row['stop_durable'])
        self.assertIsNone(status['controller_pid']);self.assertFalse(status['business_ready'])
        self.assertEqual(value.persistent()['desired'],'STOPPED')
        self.assertEqual(value.persistent()['generations'],dict(controller=0,model=0,gateway=0))
        value.stop();self.owner.check()

    def test_unheld_descriptor_is_rejected_before_controller_start(self):
        value=self.service(kind='unheld')
        self.assertNotEqual(value.process.wait(timeout=3),0)
        self.assertIn('not continuously held',(value.directory/'native.log').read_text())
        self.assertEqual(value.persistent()['revision'],0)

    def test_locked_other_file_cannot_substitute_for_service_instance(self):
        value=self.service(kind='wrong')
        self.assertNotEqual(value.process.wait(timeout=3),0)
        self.assertIn('not continuously held',(value.directory/'native.log').read_text())
        self.assertEqual(value.persistent()['revision'],0)

    def test_daemon_detach_cannot_escape_outer_instance_lifecycle(self):
        value=self.service(daemon=True)
        self.assertNotEqual(value.process.wait(timeout=3),0)
        self.assertIn('requires foreground lifecycle',(value.directory/'native.log').read_text())
        self.assertEqual(value.persistent()['revision'],0)


if __name__=='__main__':unittest.main(module=None)
