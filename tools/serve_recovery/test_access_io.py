"""CPU-only credential I/O isolation with owned processes and real native HTTP."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_io as io, access_policy as policy
from tools.serve_recovery import access_store, journal, ledger
from tools.serve_recovery.test_access_ingress import ALPHA, BETA, NEW, Stack

ROOT = Path(__file__).resolve().parents[2]


class AccessIoTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.q4t-work/serve-access-20261109/io-fixtures'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        info = self.state.stat()
        self.identity = [info.st_dev, info.st_ino]
        self.service_id = str(uuid.uuid4())
        self.initial = policy.from_keys([dict(id='alpha', api_keys=[ALPHA]),
                                         dict(id='beta', api_keys=[BETA])])
        access_store.initialize_bound(access_store.binding(self.state, self.service_id), self.initial)

    def open(self, **options):
        return access_store.Store(self.state / 'client-access', self.service_id,
                                  self.identity, **options)

    def close_reader(self, reader):
        end = time.monotonic() + 3
        while not reader.close() and time.monotonic() < end:time.sleep(.01)
        self.assertIsNone(reader.process, 'owned CPU reader must be reaped')
        self.assertIsNone(reader.claim)

    def reader(self, **options):
        reader = io.Reader(self.state, self.service_id, self.identity, **options)
        self.addCleanup(self.close_reader, reader)
        return reader

    def until(self, reader, predicate, seconds=4):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            reader.step()
            if predicate():return
            time.sleep(.01)
        self.fail('CPU reader deadline: ' + str(reader.status()))

    def test_real_journal_new_revision_and_large_fragmented_history(self):
        reader = self.reader()
        self.until(reader, lambda: reader.available)
        self.assertEqual(reader.value, self.initial)
        pid = reader.process.pid
        changed = policy.from_keys([dict(id='alpha', api_keys=[NEW]),
                                    dict(id='beta', api_keys=[BETA])], self.initial)
        for i in range(policy.MAX_HISTORY - len(changed['key_owners'])):
            changed['key_owners'][hashlib.sha256(f'old-{i}'.encode()).hexdigest()] = 'alpha'
        with self.open() as writer:writer.transact(str(uuid.uuid4()), 0, changed)
        reader.next_read = 0
        self.until(reader, lambda: reader.available and reader.value['revision'] == 1)
        self.assertEqual(reader.value, changed)
        self.assertEqual(reader.process.pid, pid)
        self.assertEqual(reader.started, 1)
        self.assertGreater(len(ledger.canonical(changed)), 256 * 1024)
        self.assertFalse(reader.status()['model_actions'])

    def test_writer_busy_and_corrupt_policy_keep_one_worker(self):
        reader = self.reader()
        self.until(reader, lambda: reader.available)
        pid = reader.process.pid
        with self.open():
            reader.next_read = 0
            self.until(reader, lambda: reader.error == 'writer_busy')
            self.assertIsNone(reader.value)
        path = self.state / 'client-access/policy.json';saved = path.read_bytes()
        path.write_bytes(b'{')
        reader.next_read = 0
        self.until(reader, lambda: reader.error == 'state_invalid')
        self.assertIsNone(reader.value)
        path.write_bytes(saved)
        reader.next_read = 0
        self.until(reader, lambda: reader.available)
        self.assertEqual(reader.process.pid, pid)
        self.assertEqual((reader.started, reader.reaped), (1, 0))

    def test_stopped_reader_timeout_reap_and_fixed_launch_budget(self):
        reader = self.reader()
        for attempt in range(3):
            self.until(reader, lambda: reader.available)
            owned = reader.process
            os.kill(owned.pid, signal.SIGSTOP)
            reader.next_read = 0
            self.until(reader, lambda: reader.error == 'reader_timeout')
            self.assertTrue(reader.status()['worker_unreaped'])
            self.assertFalse(reader.available)
            self.until(reader, lambda: reader.process is None)
            self.assertEqual(owned.returncode, -signal.SIGKILL)
            self.assertEqual(reader.reaped, attempt + 1)
        self.until(reader, lambda: reader.error == 'reader_budget')
        self.assertEqual(reader.started, 3)
        self.assertIsNone(reader.process)

    def test_competing_service_claim_never_spawns_another_worker(self):
        reader = self.reader();self.until(reader, lambda: reader.available)
        other = self.reader();other.step()
        self.assertEqual(other.error, 'reader_start_failed')
        self.assertEqual(other.started, 0)
        self.assertIsNone(other.process)
        self.close_reader(reader)
        other.next_start = 0;self.until(other, lambda: other.available)
        self.assertEqual(other.started, 1)

    def test_parent_exit_before_module_load_retains_inherited_claim(self):
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        owned = None
        try:
            result = subprocess.run([sys.executable, '-B', '-m', __name__,
                '--orphan-parent', str(self.state), self.service_id, *map(str, self.identity)],
                cwd=ROOT, capture_output=True, text=True, timeout=4, check=True)
            owned = json.loads(result.stdout)
            self.assertEqual(journal.process_start(owned['pid']), owned['start'])
            reader = self.reader();reader.step()
            self.assertEqual(reader.started, 0)
            self.assertEqual(reader.error, 'reader_start_failed')
            os.kill(owned['pid'], signal.SIGKILL)
            self.assertEqual(os.waitpid(owned['pid'], 0)[0], owned['pid'])
            owned = None
            reader.next_start = 0;self.until(reader, lambda: reader.available)
            self.assertEqual(reader.started, 1)
        finally:
            if owned is not None:
                os.kill(owned['pid'], signal.SIGKILL);os.waitpid(owned['pid'], 0)
            self.assertEqual(libc.prctl(36, previous.value, 0, 0, 0), 0)

    def test_wrong_sequence_truncated_frame_and_blocked_send_are_bounded(self):
        # Direct protocol injection; no process or storage double claims truth.
        for raw in (ledger.canonical(dict(sequence=2, status='ERROR', bytes=0,
                                          sha256='', error='writer_busy')),
                    b'x' * (io.PACKET + 1)):
            reader = self.reader()
            left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            try:
                reader.socket = left;left.setblocking(False)
                reader.phase = 'header';reader.sequence = 1
                right.send(raw)
                with self.assertRaises(ValueError):reader.receive()
                self.assertIsNone(reader.value)
            finally:right.close();reader.close()
        reader = self.reader();self.until(reader, lambda: reader.available)
        real = reader.socket
        wrapper = mock.Mock(wraps=real)
        wrapper.send.side_effect = BlockingIOError()
        reader.socket = wrapper;reader.next_read = 0
        before = reader.sequence
        reader.step();reader.step()
        self.assertEqual(reader.phase, 'send')
        self.assertEqual(reader.sequence, before + 1)
        # Retain ownership even if the kernel does not confirm a kill.
        owned = reader.process
        with mock.patch.object(owned, 'kill', side_effect=PermissionError()):
            reader.disable();reader.step()
            self.assertTrue(reader.status()['worker_unreaped'])
            self.assertIsNotNone(reader.claim)
            self.assertEqual(reader.started, 1)
        reader.socket = real

    def test_private_fds_and_missing_file_leave_native_snapshot_serving(self):
        marker = self.state / 'must-not-inherit';marker.write_text('CPU only')
        fd = os.open(marker, os.O_RDWR)
        os.set_inheritable(fd, True)
        self.addCleanup(os.close, fd)
        stack = Stack(self.state);self.addCleanup(stack.close)
        os.set_inheritable(stack.admission.fileno(), True)
        admission_target = os.readlink(f'/proc/self/fd/{stack.admission.fileno()}')
        reader = self.reader();self.until(reader, lambda: reader.available)
        stack.publish(reader.value)
        own_pid = reader.process.pid;native_pid = stack.native.pid
        targets = []
        for entry in Path(f'/proc/{own_pid}/fd').iterdir():
            try:targets.append(os.readlink(entry))
            except FileNotFoundError:pass
        self.assertNotIn(str(marker), targets)
        self.assertNotIn(admission_target, targets)
        self.assertEqual(stack.request()[0], 200)
        (self.state / 'client-access/policy.json').unlink()
        reader.next_read = 0
        self.until(reader, lambda: not reader.available)
        self.assertIsNone(reader.value)
        self.assertEqual(stack.request()[0], 200)
        self.assertEqual(stack.native.pid, native_pid)
        self.assertEqual(reader.process.pid, own_pid)
        self.assertEqual(reader.started, 1)
        stack.stop_native();stack.start()
        self.assertEqual(stack.request()[0], 503)


def orphan_parent():
    # Hold the exact inherited claim before the real reader can import anything.
    state, service_id, device, inode = sys.argv[2:]
    spawn = io.subprocess.Popen
    def before_import(args, **options):
        shim = ('import os,signal,sys;os.kill(os.getpid(),signal.SIGSTOP);'
                'os.execv(sys.argv[1],sys.argv[1:])')
        return spawn([sys.executable, '-I', '-B', '-c', shim, *args], **options)
    reader = io.Reader(state, service_id, [int(device), int(inode)])
    with mock.patch.object(io.subprocess, 'Popen', side_effect=before_import):reader.start()
    if reader.process is None:raise RuntimeError('CPU orphan fixture failed')
    print(json.dumps(dict(pid=reader.process.pid, start=journal.process_start(reader.process.pid))), flush=True)
    os._exit(0)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--orphan-parent':orphan_parent()
    else:unittest.main()
