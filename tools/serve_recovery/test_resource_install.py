"""Root-owned, CPU-only installation tests on new private kernel domains."""
import contextlib
import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import device_domain, resource_install as install
from tools.serve_recovery import resource_kernel as kernel, service, store


def worker():
    operation, directory, uid, gid = sys.argv[2:6]
    os.setgroups([]); os.setresgid(int(gid), int(gid), int(gid))
    os.setresuid(int(uid), int(uid), int(uid))
    value = install.read_public(Path(directory))
    with kernel.Scope(value['record']) as scope:
        if operation == 'join':
            fd = int(sys.argv[6]); scope.join_self('service', fd); os.close(fd)
        print(json.dumps(dict(uid=os.geteuid(), domain_id=value['domain_id'],
                              kernel_verified=True)), flush=True)
        if operation == 'join': time.sleep(10)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.geteuid(), 0, 'explicit root CPU validation required')
        self.uid, self.gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
        self.assertGreater(self.uid, 0)
        self.temporary = tempfile.TemporaryDirectory(prefix='q4t-resource-install-', dir='/run')
        self.parent = Path(self.temporary.name); self.parent.chmod(0o755)
        self.directory = self.parent/'installation'
        self.sid = str(uuid.uuid4()); self.selected = kernel.policy(32, 64, 32)
        self.roots = set(); self.processes = []
        self.addCleanup(self.cleanup)
        original = kernel.create
        def create(sid, domain_id, *args, **kwargs):
            self.roots.add(kernel.CGROUP/('q4t-resource-'+domain_id))
            return original(sid, domain_id, *args, **kwargs)
        self.patch = mock.patch.object(kernel, 'create', side_effect=create)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def cleanup(self):
        for process in self.processes:
            if process.poll() is None: process.terminate()
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
            if process.stdout: process.stdout.close()
            if process.stderr: process.stderr.close()
        for root in self.roots:
            if not root.exists(): continue
            for role in kernel.NAMES:
                path = root/role
                if path.exists():
                    self.assertIn('populated 0', (path/'cgroup.events').read_text().splitlines())
                    path.rmdir()
            root.rmdir()
        self.temporary.cleanup()

    def initialize(self, **changes):
        options = dict(service_id=self.sid, uid=self.uid, gid=self.gid, selected=self.selected)
        options.update(changes)
        return install.initialize(self.directory, **options)

    def run_worker(self, operation):
        value = install.read_public(self.directory)
        with kernel.Scope(value['record']) as scope:
            fd = scope.join_descriptor('service') if operation == 'join' else None
        try:
            argv = [sys.executable, '-I', '-B', __file__, '--worker', operation,
                    str(self.directory), str(self.uid), str(self.gid)]
            if fd is not None: argv.append(str(fd))
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env={}, pass_fds=() if fd is None else (fd,))
            self.processes.append(process)
            ready, _, _ = select.select([process.stdout], [], [], 3)
            self.assertTrue(ready, 'resource CPU child entry deadline')
            raw = process.stdout.readline(8193)
            if not raw:
                self.fail(process.communicate(timeout=2)[1].decode())
            return process, json.loads(raw)
        finally:
            if fd is not None: os.close(fd)

    def test_cli_init_status_and_repeat_preserve_original_kernel_identity(self):
        arguments = ['resource-domain', 'init', '--directory', str(self.directory),
                     '--service-id', self.sid, '--runtime-uid', str(self.uid),
                     '--runtime-gid', str(self.gid), '--memory-high-mib', '32',
                     '--memory-max-mib', '64', '--tasks-max', '32']
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(service.main(arguments), 0)
        first = json.loads(out.getvalue()); original = install.load(self.directory)
        self.assertEqual(first, self.initialize())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(service.main(['resource-domain', 'status', '--directory', str(self.directory)]), 0)
        result = json.loads(out.getvalue())
        self.assertTrue(result['kernel_verified']); self.assertTrue(result['service_empty'])
        self.assertTrue(result['outer_empty']); self.assertFalse(result['model_actions'])
        self.assertFalse(result['device_authorized']); self.assertEqual(original, install.load(self.directory))

    def test_first_private_write_interruption_resumes_without_published_authority(self):
        def partial(fd, raw):
            os.write(fd, raw[:len(raw)//2]); raise OSError('injected first intent write')
        with mock.patch.object(store.Store, 'write_all', side_effect=partial), self.assertRaises(OSError):
            self.initialize()
        self.assertFalse(self.roots); self.assertFalse((self.directory/install.PUBLIC).exists())
        self.assertFalse((self.directory/'admin'/install.PRIVATE).exists())
        result = self.initialize()
        self.assertTrue(result['admission_enabled']); self.assertEqual(result['service_id'], self.sid)

    def test_partial_kernel_setup_resumes_only_same_empty_unpublished_domain(self):
        original = kernel.write
        def write(fd, name, amount):
            original(fd, name, amount)
            if name == 'memory.high': raise OSError('injected partial controller installation')
        with mock.patch.object(kernel, 'write', side_effect=write), self.assertRaises(OSError):
            self.initialize()
        before = install.load(self.directory); root = next(iter(self.roots)); identity = kernel.identity(root)
        self.assertEqual(before['phase'], 'prepared')
        with self.assertRaisesRegex(ValueError, 'not enabled'): install.read_public(self.directory)
        result = install.finish(self.directory)
        self.assertEqual(before['domain_id'], result['domain_id'])
        self.assertEqual(identity, result['record']['root_identity']); self.assertEqual(len(self.roots), 1)

    def test_ready_publication_loss_repairs_copy_without_reinitializing_private_history(self):
        original = install.publish
        def publish(directory, value):
            if value['phase'] == 'ready': raise OSError('injected ready publication')
            original(directory, value)
        with mock.patch.object(install, 'publish', side_effect=publish), self.assertRaises(OSError):
            self.initialize()
        before = install.load(self.directory); self.assertEqual(before['phase'], 'ready')
        with self.assertRaisesRegex(ValueError, 'not enabled'): install.read_public(self.directory)
        result = install.finish(self.directory)
        self.assertEqual(before, install.load(self.directory)); self.assertTrue(result['admission_enabled'])
        self.assertEqual(before['record'], install.read_public(self.directory)['record'])

    def test_lost_published_private_authority_never_bootstraps_again(self):
        self.initialize(); path = self.directory/'admin'/install.PRIVATE
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'authority is missing'): self.initialize()
        self.assertEqual(len(self.roots), 1)
        self.assertFalse(path.exists())

    def test_active_removal_revokes_entry_without_signalling_payload(self):
        self.initialize(); process, result = self.run_worker('join')
        with self.assertRaisesRegex(ValueError, 'explicit stop'): install.remove(self.directory)
        self.assertIsNone(process.poll()); self.assertEqual(result['uid'], self.uid)
        self.assertEqual(install.load(self.directory)['phase'], 'revoking')
        with self.assertRaisesRegex(ValueError, 'not enabled'): install.read_public(self.directory)
        process.terminate(); process.wait(timeout=2)
        removed = install.remove(self.directory)
        self.assertTrue(removed['removed']); self.assertTrue(removed['history_retained'])
        self.assertEqual(removed, install.remove(self.directory))
        self.assertFalse(Path(removed['record']['root']).exists())

    def test_partial_removal_checkpoint_resumes_and_rejects_replaced_leaf(self):
        value = self.initialize(); root = Path(value['record']['root'])
        original = Path.rmdir
        def rmdir(path):
            original(path)
            if path == root/'outer': raise OSError('injected after first leaf removal')
        with mock.patch.object(Path, 'rmdir', rmdir), self.assertRaises(OSError):
            install.remove(self.directory)
        self.assertEqual(install.load(self.directory)['phase'], 'empty')
        (root/'outer').mkdir()
        with self.assertRaisesRegex(ValueError, 'leaf replaced'): install.remove(self.directory)
        (root/'outer').rmdir()
        self.assertTrue(install.remove(self.directory)['removed']); self.assertFalse(root.exists())

    def test_identity_drift_and_lock_contention_reject_while_public_is_readable_unprivileged(self):
        value = self.initialize(); before = install.load(self.directory)
        for change in (dict(service_id=str(uuid.uuid4())), dict(uid=self.uid+1),
                       dict(selected=kernel.policy(64, 64, 32))):
            with self.assertRaisesRegex(ValueError, 'selection changed'): self.initialize(**change)
        with install.exclusive(self.directory), self.assertRaises(BlockingIOError):
            install.finish(self.directory)
        process, result = self.run_worker('read')
        self.assertEqual(process.wait(timeout=2), 0)
        self.assertEqual(result['domain_id'], value['domain_id']); self.assertEqual(result['uid'], self.uid)
        self.assertEqual(before, install.load(self.directory))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker': worker()
    else: unittest.main(module=None)
