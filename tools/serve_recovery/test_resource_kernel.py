"""Owned real cgroup-v2 CPU tests; no device rules, NVML or GPU operations."""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import outer_tree, resource_kernel as kernel


def worker():
    record, role, fd, uid, gid, mode = json.loads(sys.argv[2]), sys.argv[3], *map(int, sys.argv[4:7]), sys.argv[7]
    os.setgroups([]); os.setresgid(gid, gid, gid); os.setresuid(uid, uid, uid)
    with kernel.Scope(record) as scope:
        result = scope.join_self(role, fd); os.close(fd)
        result.update(pid=os.getpid(), uid=os.geteuid(), physical_ram_limit=False)
        if mode == 'pids':
            count = 0
            try:
                for _ in range(16):
                    child = os.fork()
                    if child == 0: time.sleep(10); os._exit(0)
                    count += 1
            except BlockingIOError: result['task_limit_reached'] = True
            result['forked'] = count
        print(json.dumps(result), flush=True)
        if mode == 'oom':
            memory = bytearray(128*1024**2)
            # calloc may map untouched zero pages; exercise actual residency.
            memory[::4096] = b'x'*(len(memory)//4096)
        time.sleep(10)


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.geteuid(), 0, 'explicit privileged CPU validation required')
        self.uid, self.gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
        self.assertGreater(self.uid, 0)
        # The OOM case isolates the hard cap from memory.high throttling.
        high = 64 if self._testMethodName == 'test_memory_oom_is_confined_to_payload_and_recorded_by_kernel' else 32
        self.record = kernel.create(str(uuid.uuid4()), str(uuid.uuid4()), self.uid, kernel.policy(high, 64, 8))
        self.scope = kernel.Scope(self.record)
        self.tree = outer_tree.Tree().__enter__(); self.processes = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        try:
            if self.tree.root is not None: self.tree.drain(term_seconds=.1, kill_seconds=1)
        finally:
            self.tree.close(); self.scope.close()
            root = Path(self.record['root'])
            for role in kernel.NAMES:
                self.assertIn('populated 0', (root/role/'cgroup.events').read_text().splitlines())
                (root/role).rmdir()
            root.rmdir()

    def start(self, role='service', mode='sleep'):
        fd = self.scope.join_descriptor(role)
        try:
            command = [sys.executable, '-I', '-B', __file__, '--worker', json.dumps(self.record),
                       role, str(fd), str(self.uid), str(self.gid), mode]
            if self.tree.root is None:
                # Tree stdout is fixed stdio; a private pipe captures one short
                # CPU readiness message without introducing a reader process.
                read_fd, write_fd = os.pipe()
                try:
                    self.tree.launch(command, env={}, pass_fds=(fd,), stdio=write_fd)
                    process = self.tree.root
                finally: os.close(write_fd)
                stream = os.fdopen(read_fd, 'rb')
            else:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, env={}, pass_fds=(fd,))
                stream = process.stdout
            self.processes.append(process)
            import select
            ready, _, _ = select.select([stream], [], [], 3)
            self.assertTrue(ready, 'CPU resource entry deadline')
            raw = stream.readline(8193); stream.close()
            value = json.loads(raw)
            self.assertEqual(value['uid'], self.uid); self.assertEqual(value['joined'], role)
            return process, value
        finally: os.close(fd)

    def test_actual_kernel_limits_and_identity_do_not_claim_physical_ram_bound(self):
        self.assertTrue(self.scope.empty()); self.scope.check()
        self.assertFalse(self.record['policy']['physical_ram_limit'])
        for name, expected in kernel.limits(self.record['policy'], 'service').items():
            self.assertEqual(kernel.read(self.scope.fds['service'], name), str(expected))
        with self.assertRaises(FileExistsError):
            kernel.create(self.record['service_id'], self.record['domain_id'], self.uid, self.record['policy'])

    def test_unprivileged_self_entry_and_service_kill_leave_outer_observer_alive(self):
        observer, _ = self.start('outer'); payload, _ = self.start('service')
        self.assertFalse(self.scope.empty('outer')); self.assertFalse(self.scope.empty())
        self.assertTrue(self.scope.kill_service()['kernel_empty'])
        self.assertEqual(payload.wait(timeout=2), -signal.SIGKILL); self.assertIsNone(observer.poll())
        self.assertFalse(self.scope.empty('outer')); self.assertTrue(self.scope.empty())

    def test_real_task_limit_and_subtree_kill_include_forked_descendants(self):
        process, result = self.start(mode='pids')
        self.assertTrue(result['task_limit_reached']); self.assertLess(result['forked'], 16)
        events = kernel.counters(kernel.read(self.scope.fds['service'], 'pids.events'))
        self.assertGreater(events['max'], 0)
        self.assertTrue(self.scope.kill_service()['kernel_empty'])
        self.assertEqual(process.wait(timeout=2), -signal.SIGKILL)

    def test_memory_oom_is_confined_to_payload_and_recorded_by_kernel(self):
        observer, _ = self.start('outer'); process, _ = self.start(mode='oom')
        self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
        events = kernel.counters(kernel.read(self.scope.fds['service'], 'memory.events'))
        self.assertGreater(events['oom_kill'], 0)
        self.assertIsNone(observer.poll()); self.assertTrue(self.scope.empty())

    def test_missing_population_observation_never_grants_cleanup_or_sends_a_signal(self):
        process, _ = self.start(); original = kernel.read
        def read(fd, name, *args):
            if name == 'cgroup.events': raise OSError('injected CPU population read failure')
            return original(fd, name, *args)
        with mock.patch.object(kernel, 'read', side_effect=read), self.assertRaises(OSError):
            self.scope.empty()
        self.assertIsNone(process.poll()); self.assertFalse(self.scope.empty())

    def test_replaced_kernel_leaf_and_namespace_identity_are_rejected(self):
        changed = copy.deepcopy(self.record); changed['namespaces']['cgroup'][1] += 1
        with self.assertRaisesRegex(ValueError, 'namespace'): kernel.Scope(changed)
        leaf = Path(self.record['root'])/'service'; leaf.rmdir(); leaf.mkdir()
        with self.assertRaisesRegex(ValueError, 'domain changed'): self.scope.check()

    def test_regular_directory_or_wrong_entry_descriptor_cannot_be_a_kernel_domain(self):
        fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaisesRegex(ValueError, 'cgroup v2'): kernel.filesystem(fd)
            with self.assertRaisesRegex(ValueError, 'capability'): self.scope.join_self('service', fd)
        finally: os.close(fd)
        self.assertTrue(self.scope.empty()); self.assertTrue(self.scope.empty('outer'))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker': worker()
    else: unittest.main(module=None)
