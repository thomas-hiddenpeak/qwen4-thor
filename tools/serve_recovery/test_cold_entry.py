"""Public native entry with a private CPU lease; never the real GPU lease."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import cold_admission, device_domain as domain, host_install as host
from tools.serve_recovery import ledger, store
from tools.serve_recovery import test_cold_issuer as fixtures


class EntryTests(fixtures.AuthorityTests):
    def setUp(self):
        super().setUp()
        self.entry_path = Path(self.lease_path())
        self.entry_fd = os.open(self.entry_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.fchown(self.entry_fd, self.uid, self.gid)
        fcntl.flock(self.entry_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, self.entry_fd)

    def launch_entry(self, kind, *, activate=False, daemon=False):
        argv = [sys.executable, '-I', '-B', __file__, '--fixture-entry', kind,
            str(self.device), str(self.host), self.fixture['digest'], str(self.entry_path),
            str(self.entry_fd), 'start' if activate else 'enter', 'daemon' if daemon else 'foreground']
        env = dict(host.environment(), Q4T_MANAGED_LEASE_FD=str(self.entry_fd))
        process = subprocess.Popen(argv, env=env, pass_fds=(self.entry_fd,),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process); return process

    def complete(self, process):
        raw, error = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 0, error.decode())
        return json.loads(raw)

    def core(self):
        return json.loads((Path(self.fixture['state'])/store.STATE).read_text())['state']

    def assert_original_lease(self):
        value = json.loads((Path(self.fixture['package']).parent/'cpu-managed-lease.json').read_text())
        held = os.fstat(self.entry_fd)
        self.assertEqual(value, dict(uid=self.uid, device=held.st_dev, inode=held.st_ino))

    def test_resource_bound_public_entry_uses_protected_host_and_original_lease(self):
        before = self.state_files()
        result = self.complete(self.launch_entry('device'))
        self.assertEqual(result['state'], 'STOPPED'); self.assertTrue(result['cleanup_confirmed'])
        self.assertEqual(before, self.state_files()); self.assert_original_lease()

    def test_public_host_start_activates_only_explicitly_and_stop_keeps_zero_roles(self):
        for kind in ('host', 'device'):
            with self.subTest(entry=kind):
                self.assertEqual(self.core()['desired'], 'STOPPED')
                process = self.launch_entry(kind, activate=True); deadline = time.monotonic()+6
                while self.core()['desired'] != 'RUNNING':
                    if process.poll() is not None: self.fail(process.communicate(timeout=1)[1].decode())
                    self.assertLess(time.monotonic(), deadline); time.sleep(.02)
                self.assert_original_lease()
                self.assertFalse(any(self.core()['generations'].values()))
                # Unarmed native issuer blocks before any Guardian/model dispatch.
                self.assertTrue(self.enter(operation='stop')['cleanup_confirmed'])
                result = self.complete(process); self.assertEqual(result['state'], 'STOPPED')
                self.assertFalse(any(self.core()['generations'].values()))

    def test_resource_bound_daemon_resume_keeps_stopped_and_forwarded_lease(self):
        result = self.complete(self.launch_entry('device', daemon=True))
        self.assertTrue(result['startup_confirmed']); self.assertEqual(result['state'], 'STOPPED')
        self.assert_original_lease(); self.assertFalse(any(self.core()['generations'].values()))

    def test_wrong_manifest_arbitrary_command_and_lease_path_never_exec(self):
        with mock.patch.object(domain.os, 'execve') as execute:
            for digest, command, path in [('a'*64, [], self.entry_path),
                    (self.fixture['digest'], ['/bin/false'], self.entry_path),
                    (self.fixture['digest'], [], self.entry_path.with_name('not-the-lease'))]:
                with self.subTest(command=command, path=str(path)), self.assertRaises((OSError, ValueError)):
                    domain.enter(self.device, self.entry_fd, path, digest, command, host_directory=self.host)
            execute.assert_not_called()
        self.assertEqual(self.core()['desired'], 'STOPPED')

    def test_host_entry_rejects_bad_fd_and_wrong_owner_without_weak_fallback(self):
        value = host.load(self.host)
        for raw in ('', '1', 'abc', '99999999'):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {'Q4T_MANAGED_LEASE_FD': raw}):
                with self.assertRaises(ValueError): host.entry_environment(value)
        os.fchown(self.entry_fd, 0, 0)
        try:
            with self.assertRaisesRegex(ValueError, 'another user'):
                host.entry_environment(value, lease_fd=self.entry_fd, lease_path=self.entry_path)
        finally: os.fchown(self.entry_fd, self.uid, self.gid)
        env = host.entry_environment(value, lease_fd=self.entry_fd, lease_path=self.entry_path)
        self.assertEqual(set(env), {*host.environment(), 'Q4T_MANAGED_LEASE_FD'})

    def test_explicit_descriptor_adapter_keeps_sealed_receipt_and_unconsumed_token(self):
        token, write_token = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.addCleanup(os.close, token); self.addCleanup(os.close, write_token)
        os.fchmod(token, 0o600); nonce = b'cpu-one-use-token'; os.write(write_token, nonce)
        pipe = os.fstat(token); held = os.fstat(self.entry_fd)
        receipt = os.memfd_create('q4t-cpu-cold-receipt', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        try:
            os.fchmod(receipt, 0o600)
            os.write(receipt, ledger.canonical(dict(format='q4t-device-admission-v1',
                claims=dict(service_id=self.sid, manifest_sha256=self.fixture['digest'], runtime_uid=self.uid,
                    lease_device=held.st_dev, lease_inode=held.st_ino),
                pipe_device=pipe.st_dev, pipe_inode=pipe.st_ino, nonce='a'*64)))
            fcntl.fcntl(receipt, fcntl.F_ADD_SEALS, 15)
            readonly = os.open('/proc/self/fd/'+str(receipt), os.O_RDONLY | os.O_CLOEXEC)
        finally: os.close(receipt)
        self.addCleanup(os.close, readonly)
        extra = [os.dup(self.entry_fd), os.dup(self.entry_fd)]
        for fd in extra: self.addCleanup(os.close, fd)
        descriptors = (readonly, token, *extra)
        result = subprocess.run([sys.executable, '-I', '-B', __file__, '--adapter', self.sid,
            self.fixture['digest'], str(self.entry_fd), *map(str, descriptors)],
            user=self.uid, group=self.gid, extra_groups=[], env=host.environment(),
            pass_fds=(self.entry_fd, *descriptors), capture_output=True, text=True, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(checked=True, duplicated=4))
        self.assertEqual(os.read(token, 1024), nonce)
        # Only the native Guardian is allowed to consume and validate the BPF
        # link/program. These two fake regular FDs explicitly test forwarding.
        with mock.patch.dict(os.environ, {cold_admission.ENVIRONMENT: ','.join(map(str, descriptors))}):
            with self.assertRaisesRegex(ValueError, 'mixed'):
                cold_admission.configuration(self.sid, self.fixture['digest'], self.entry_fd,
                                             descriptors=descriptors)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--fixture-entry':
        kind, device, directory, digest, path, fd, operation, mode = sys.argv[2:]
        if kind == 'host':
            argv = [operation, '--directory', directory]
            if mode == 'daemon': argv.append('--daemon')
            raise SystemExit(host.main(argv))
        argv = ['enter', '--directory', device, '--host-directory', directory,
            '--manifest-sha256', digest, '--lease-path', path, '--lease-fd', fd]
        if operation == 'start': argv.append('--activate')
        if mode == 'daemon': argv.append('--daemon')
        raise SystemExit(domain.run(argv))
    elif len(sys.argv) > 1 and sys.argv[1] == '--adapter':
        sid, digest, lease, *descriptors = sys.argv[2:]
        config, copies = cold_admission.configuration(sid, digest, int(lease), descriptors=list(map(int, descriptors)))
        try:
            assert len(copies) == 4 and not set(copies) & set(map(int, descriptors))
            assert config['manifest_sha256'] == digest
            print(json.dumps(dict(checked=True, duplicated=len(copies))))
        finally:
            for fd in copies: os.close(fd)
    else: unittest.main(module=None)
