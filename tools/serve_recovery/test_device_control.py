"""Actual native root IPC and CPU cgroups; no host-root rule or NVIDIA access."""
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
import uuid

from tools.serve_recovery import device_control as c, ledger


class ControlTests(unittest.TestCase):
    def setUp(self):
        ledger.require(os.geteuid() == 0, 'explicit root CPU check required')
        root = Path(__file__).resolve().parents[2]
        base = root/'.q4t-work/serve-device-deployment-20261112/control-tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.lease_path = self.path/'lease'
        self.lease = os.open(self.lease_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        self.addCleanup(os.close, self.lease)
        self.uid, self.gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
        os.fchown(self.lease, self.uid, self.gid)
        fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.binary = Path(os.environ['Q4T_DEVICE_CONTROL_BINARY']).resolve(strict=True)
        self.control = c.Control(self.binary, self.lease, self.lease_path)
        suffix = str(os.getpid())+'-'+uuid.uuid4().hex
        self.group = Path('/sys/fs/cgroup')/('q4t-control-cpu-'+suffix)
        self.group.mkdir(mode=0o755); self.addCleanup(self.group.rmdir)
        self.allowed = self.group/'allowed'
        self.allowed.mkdir(mode=0o755); self.addCleanup(self.allowed.rmdir)
        self.pin_dir = Path('/sys/fs/bpf')/('q4t-control-cpu-'+suffix)
        self.pin_dir.mkdir(mode=0o700); self.addCleanup(self.pin_dir.rmdir)
        self.pin = self.pin_dir/'policy'
        self.addCleanup(lambda: self.pin.unlink(missing_ok=True))
        self.target_fd = os.open(self.group, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        self.allowed_fd = os.open(self.allowed, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        self.addCleanup(os.close, self.target_fd); self.addCleanup(os.close, self.allowed_fd)
        self.args = dict(target_fd=self.target_fd, allowed_fd=self.allowed_fd,
                         devices=[dict(major=1, minor=5, all_minors=False)], pin=str(self.pin))
        self.inherited = (self.target_fd, self.allowed_fd)

    def attach(self):
        reply = self.control.call('attach', self.args, inherited=self.inherited)
        self.addCleanup(reply.close)
        return reply

    def admission(self, identity):
        lease = os.fstat(self.lease)
        return dict(service_id=str(uuid.uuid4()), domain_id=str(uuid.uuid4()),
            boot_id=str(uuid.uuid4()), manifest_sha256='a'*64,
            qualification_sha256='b'*64, journal_receipt_sha256='c'*64,
            ticket_revision=2**53+1, issued_ns=1, expires_ns=300000000001,
            runtime_uid=self.uid, lease_device=lease.st_dev, lease_inode=lease.st_ino,
            filter=identity)

    def test_native_attachment_survives_adapter_exit_and_adopts_then_removes(self):
        attached = self.attach()
        identity = attached.result
        self.assertEqual(identity['target_cgroup_id'], self.group.stat().st_ino)
        self.assertEqual(identity['allowed_cgroup_id'], self.allowed.stat().st_ino)
        self.assertIsNone(self.control.pending)
        attached.close()
        self.assertTrue(self.pin.exists())
        args = dict(self.args, identity=identity)
        with self.control.call('adopt', args, inherited=self.inherited) as adopted:
            self.assertEqual(adopted.result, identity)
            self.assertTrue(all(not os.get_inheritable(fd) for fd in adopted.fds))
        with self.control.call('remove', args, inherited=self.inherited) as removed:
            self.assertEqual(removed.result, 'removed')
            self.assertEqual(removed.fds, [])
        self.assertFalse(self.pin.exists())

    def test_native_issuer_returns_immutable_root_receipt_and_one_use_pipe(self):
        attached = self.attach()
        claims = self.admission(attached.result)
        args = dict(link_fd=attached.fds[0], program_fd=attached.fds[1], admission=claims)
        with self.control.call('issue', args, inherited=attached.fds) as issued:
            receipt, token = issued.fds
            value = json.loads(os.pread(receipt, 8192, 0))
            self.assertEqual(value['claims'], claims)
            self.assertEqual(os.fstat(receipt).st_uid, 0)
            self.assertEqual(os.fstat(token).st_uid, 0)
            self.assertEqual(fcntl.fcntl(receipt, fcntl.F_GET_SEALS), 15)
            self.assertEqual(os.read(token, 33).decode(), value['nonce'])
            self.assertEqual(os.read(token, 1), b'')
            with self.assertRaises(OSError): os.write(receipt, b'x')
            with self.assertRaises(OSError): os.write(token, b'x')
        self.assertIsNone(self.control.pending)

    def test_wrong_code_or_kernel_identity_never_adopts_another_rule(self):
        attached = self.attach()
        bad = dict(self.args, identity=dict(attached.result, link_id=attached.result['link_id']+1))
        with self.assertRaisesRegex(ValueError, 'rejected'):
            self.control.call('adopt', bad, inherited=self.inherited)
        bad = dict(self.args, identity=attached.result,
                   devices=[dict(major=1, minor=3, all_minors=False)])
        with self.assertRaisesRegex(ValueError, 'rejected'):
            self.control.call('remove', bad, inherited=self.inherited)
        self.assertTrue(self.pin.exists())
        with self.control.call('adopt', dict(self.args, identity=attached.result),
                               inherited=self.inherited): pass

    def test_unheld_lease_and_malformed_request_fail_before_attachment(self):
        with self.assertRaisesRegex(ValueError, 'rejected'):
            self.control.call('attach', dict(self.args, extra=True), inherited=self.inherited)
        self.assertFalse(self.pin.exists())
        fcntl.flock(self.lease, fcntl.LOCK_UN)
        with self.assertRaisesRegex(ValueError, 'rejected'):
            self.control.call('attach', self.args, inherited=self.inherited)
        self.assertFalse(self.pin.exists())
        self.assertIsNone(self.control.pending)

    def test_native_adapter_refuses_actual_unprivileged_invocation(self):
        parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            def drop():
                os.setgroups([]); os.setgid(self.gid); os.setuid(self.uid)
            child = subprocess.run([str(self.binary), '--fd', str(endpoint.fileno())],
                pass_fds=(endpoint.fileno(),), preexec_fn=drop, env={}, timeout=5,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(child.returncode, 2)
            self.assertFalse(self.pin.exists())
        finally: parent.close(); endpoint.close()

    def test_stuck_owned_adapter_is_killed_and_reaped_without_touching_lease(self):
        fake = self.path/'blocked'
        fake.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(60)\n')
        fake.chmod(0o700)
        control = c.Control(fake, self.lease, self.lease_path)
        with self.assertRaises(TimeoutError):
            control.call('attach', self.args, inherited=self.inherited, timeout=0.05)
        self.assertIsNone(control.pending)
        other = os.open(self.lease_path, os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally: os.close(other)
        self.assertFalse(self.pin.exists())


if __name__ == '__main__': unittest.main()
