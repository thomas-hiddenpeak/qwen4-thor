"""Actual CPU-only ownership, wire and budget controls before cache GPU business."""
import copy
import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from tools.serve_cache_io import acceptance as a
from tools.serve_cache_io.progress_capture import Capture
from tools.serve_recovery import progress

ROOT = Path(__file__).resolve().parents[2]


class DriverTests(unittest.TestCase):
    def test_owned_cache_directory_selection_and_birth_rejection(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.q4t-work') as directory:
            reader, writer = os.pipe()
            code = 'import os,sys; f=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY); ' \
                   'os.write(1,b"ready\\n"); os.read(int(sys.argv[2]),1)'
            child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code,
                directory, str(reader), '--q4t-cache-worker'], pass_fds=(reader,),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertTrue(select.select([child.stdout], [], [], 2)[0])
                self.assertEqual(child.stdout.readline(), b'ready\n')
                records = a.guard.processes()
                pid, owner = a.select_worker(records, os.getpid(), sys.executable, directory)
                self.assertEqual(pid, child.pid)
                stale = copy.deepcopy(records)
                stale[pid]['start_ticks'] += 1
                with self.assertRaisesRegex(RuntimeError, 'one exact owned'):
                    a.select_worker(stale, os.getpid(), sys.executable, directory)
                with self.assertRaisesRegex(RuntimeError, 'one exact owned'):
                    a.select_worker(records, 1, sys.executable, directory)
                with self.assertRaisesRegex(RuntimeError, 'one exact owned'):
                    a.select_worker(records, os.getpid(), '/bin/true', directory)
                a.guard.signal_identity(pid, owner, signal.SIGSTOP)
                end = time.monotonic()+1
                while time.monotonic() < end and a.guard.processes()[pid]['state'] != 'T':
                    time.sleep(.005)
                self.assertEqual(a.guard.processes()[pid]['state'], 'T')
                a.guard.signal_identity(pid, owner, signal.SIGCONT)
                os.write(writer, b'x')
                self.assertEqual(child.wait(timeout=2), 0)
            finally:
                os.close(reader)
                os.close(writer)
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=2)

    def test_private_progress_wire_capture_preserves_bytes_and_wait_boundary(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        identity = progress.Identity(os.getpid(), 1, 1, 1, 1,
            '00000000-0000-0000-0000-000000000001',
            '00000000-0000-0000-0000-000000000002')
        capture = Capture(left, identity)
        now = time.monotonic_ns()
        header = progress.HEADER.pack(b'Q4TPR002', 1, now, 1, identity.pid,
            1, 1, 1, 1, 1, 0, 1, 0, 0, identity.boot_id.encode(),
            identity.service_id.encode())
        # A very old operation inside file wait is not GPU execution evidence.
        row = progress.ROW.pack(3, now-200*progress.NS, 4, now-200*progress.NS,
            9, now-200*progress.NS, 4, 1, now-200*progress.NS,
            0, 0, now)
        packet = header+row
        with tempfile.TemporaryDirectory(dir=ROOT/'.q4t-work') as directory:
            path = Path(directory)/'progress.bin'
            try:
                right.send(packet)
                end = time.monotonic()+1
                while time.monotonic() < end and not capture.summary()['snapshots']:
                    time.sleep(.005)
                summary = capture.summary()
                self.assertEqual(summary['cache_wait_samples'], 1)
                self.assertIsNone(summary['error'])
            finally:
                capture.close(path)
                right.close()
            self.assertEqual(path.read_bytes(), struct.pack('<I', len(packet))+packet)
            # The cap is enforced before retaining another packet.
            a_socket, b_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            try:
                capture.socket.socket = a_socket
                capture.socket.bytes = 8*1024**2
                b_socket.send(packet)
                with self.assertRaisesRegex(RuntimeError, 'capture exhausted'):
                    capture.socket.recvmsg(progress.MAX_PACKET)
            finally:
                a_socket.close()
                b_socket.close()

    def test_failed_restore_log_and_non_refundable_request_budget(self):
        line = ('[session-cache] request=fixture event=restore prefix=0 ms=2000.1 '
                'error=cache storage time limit exceeded source=session layout=target\n')
        self.assertEqual(a.restore(line, 'fixture'), dict(prefix=0,
            error='cache storage time limit exceeded', source='session', layout='target'))
        with self.assertRaisesRegex(RuntimeError, 'one restore observation'):
            a.restore(line+line, 'fixture')
        with tempfile.TemporaryDirectory(dir=ROOT/'.q4t-work') as directory:
            stage = object.__new__(a.Stage)
            stage.out = Path(directory)
            stage.started = time.monotonic()
            stage.counts = dict(starts=0, chats=0, controls=0)
            for i in range(a.LIMITS['chats']):
                stage.budget('chats')
                recorded = json.loads((stage.out/'counts.json').read_text())
                self.assertEqual(recorded['chats'], i+1)
            with self.assertRaisesRegex(RuntimeError, 'attempt limit'):
                stage.budget('chats')
            self.assertEqual(stage.counts['chats'], 8)


if __name__ == '__main__':
    unittest.main()
