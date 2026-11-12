"""Real CPU kernel permission checks, using only newly created test processes."""
import copy
import errno
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import resource_install as install
from tools.serve_recovery import resource_kernel as kernel
from tools.serve_recovery import resource_launch as launch
from tools.serve_recovery import test_resource_install as fixtures


def denied(action, expected):
    try: action()
    except OSError as error:
        assert error.errno in expected, error
        return error.errno
    raise AssertionError('kernel accepted forbidden migration or control write')


def write_path(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try: return os.write(fd, value)
    finally: os.close(fd)


def check(mode, targets):
    directory = Path(os.environ[launch.DIRECTORY])
    public = install.read_public(directory); record = public['record']
    if mode == 'outside':
        with kernel.Scope(record) as scope:
            try: scope.join_descriptor('service')
            except ValueError: pass
            else: raise AssertionError('outside caller accepted by entry API')
        result = denied(lambda: write_path(Path(record['root'])/'service/cgroup.procs', b'0\n'),
                        {errno.EACCES, errno.EPERM})
        print(json.dumps(dict(outside_denied=result)), flush=True)
        return
    raw_fd = int(os.environ[launch.ENTRY_FD]); results = {}
    # Exercise the actual inherited FD before Capability reopens it. A root-
    # opened FD would pass inode checks but allow these outside migrations.
    for name, pid in targets.items():
        results[name] = denied(lambda: os.write(raw_fd, f'{pid}\n'.encode()),
                               {errno.EACCES, errno.EPERM})
    cap = launch.inherited(record['service_id'])
    try:
        cap.scope.join_self('service', cap.fd)
        root = Path(record['root'])
        results['parent'] = denied(lambda: write_path(root/'cgroup.procs', b'0\n'), {errno.EBUSY})
        for name in ('outer/cgroup.procs', 'outer/cgroup.threads',
                     'cgroup.threads', 'cgroup.subtree_control',
                     'service/cgroup.threads', 'service/memory.max'):
            value = b'-memory -pids\n' if name == 'cgroup.subtree_control' else b'0\n'
            results[name] = denied(lambda: write_path(root/name, value), {errno.EACCES, errno.EPERM})
        cap.scope.member('service')
        print(json.dumps(dict(kernel_denials=results, uid=os.geteuid(), joined='service')), flush=True)
    finally: cap.close()


class DelegationTests(fixtures.InstallationTests):
    def sleeping_process(self, *, user):
        code = 'import os,time; print(os.getuid(), flush=True); time.sleep(15)'
        options = dict(user=self.uid, group=self.gid, extra_groups=[]) if user else {}
        process = subprocess.Popen([sys.executable, '-I', '-B', '-c', code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={}, **options)
        self.processes.append(process)
        ready, _, _ = select.select([process.stdout], [], [], 3)
        self.assertTrue(ready); self.assertEqual(int(process.stdout.readline()), self.uid if user else 0)
        return process

    def test_actual_inherited_fd_rejects_outside_targets_and_service_escape(self):
        value = self.initialize()
        outside = self.sleeping_process(user=True); privileged = self.sleeping_process(user=False)
        before = {p.pid: Path(f'/proc/{p.pid}/cgroup').read_text() for p in (outside, privileged)}
        command = [sys.executable, '-I', '-B', __file__, '--enter', str(self.directory),
                   json.dumps(dict(same_uid=outside.pid, root_uid=privileged.pid))]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={})
        self.processes.append(process)
        out, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, error.decode())
        result = json.loads(out); self.assertEqual(result['uid'], self.uid)
        self.assertEqual(len(result['kernel_denials']), 9)
        for child in (outside, privileged):
            self.assertIsNone(child.poll())
            self.assertEqual(Path(f'/proc/{child.pid}/cgroup').read_text(), before[child.pid])
        with kernel.Scope(value['record']) as scope:
            self.assertTrue(scope.empty()); self.assertTrue(scope.empty('outer'))

    def test_unprivileged_outside_caller_cannot_pull_itself_into_installation(self):
        self.initialize()
        process = subprocess.Popen([sys.executable, '-I', '-B', __file__, '--outside'],
            user=self.uid, group=self.gid, extra_groups=[], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env={launch.DIRECTORY: str(self.directory)})
        self.processes.append(process)
        out, error = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0, error.decode())
        self.assertIn(json.loads(out)['outside_denied'], (errno.EACCES, errno.EPERM))

    def test_root_api_cannot_open_service_fd_before_uid_drop(self):
        value = self.initialize()
        with kernel.Scope(value['record']) as scope:
            with self.assertRaisesRegex(ValueError, 'permanent UID drop'):
                scope.join_descriptor('service')

    def test_permission_or_parent_controller_drift_is_rejected(self):
        value = self.initialize(); root = Path(value['record']['root'])
        with kernel.Scope(value['record']) as scope:
            for name, uid, mode in (
                    ('cgroup.procs', 0, 0o600),
                    ('service/cgroup.procs', self.uid, 0o666),
                    ('outer/cgroup.procs', self.uid, 0o644),
                    ('cgroup.threads', self.uid, 0o644),
                    ('cgroup.subtree_control', 0, 0o666),
                    ('service/memory.max', self.uid, 0o644)):
                path = root/name; original = path.stat()
                try:
                    os.chown(path, uid, -1); path.chmod(mode)
                    with self.assertRaisesRegex(ValueError, 'permission changed'): scope.check()
                finally:
                    os.chown(path, original.st_uid, original.st_gid)
                    path.chmod(stat.S_IMODE(original.st_mode))
            kernel.write(scope.fds['root'], 'cgroup.subtree_control', '-memory -pids')
            try:
                with self.assertRaisesRegex(ValueError, 'hierarchy boundary'): scope.check()
            finally:
                kernel.write(scope.fds['root'], 'cgroup.subtree_control', '+memory +pids')
                for role in kernel.NAMES:
                    for name, amount in kernel.limits(value['record']['policy'], role).items():
                        kernel.write(scope.fds[role], name, amount)
            scope.check()

    def test_old_unpublished_root_fd_formats_do_not_authorize_new_entry(self):
        self.initialize(); original = install.load(self.directory)
        legacy = copy.deepcopy(original); legacy['format'] = 'q4t-resource-installation-v1'
        with self.assertRaisesRegex(ValueError, 'installation'): install.validate(legacy)
        legacy = copy.deepcopy(original['record']); legacy['format'] = 'q4t-resource-kernel-v1'
        with self.assertRaisesRegex(ValueError, 'domain identity'): kernel.validate(legacy)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--enter':
        launch.enter(Path(sys.argv[2]), [sys.executable, '-I', '-B', __file__, '--check', sys.argv[3]])
    elif len(sys.argv) > 1 and sys.argv[1] == '--check': check('inside', json.loads(sys.argv[2]))
    elif len(sys.argv) > 1 and sys.argv[1] == '--outside': check('outside', {})
    else: unittest.main(module=None)
