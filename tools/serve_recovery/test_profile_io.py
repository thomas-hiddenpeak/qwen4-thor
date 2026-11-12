"""Actual isolated workers, durable retries and a healthy CPU model sentinel."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from tools.serve_recovery import access_store, ledger, profile_io
from tools.serve_recovery import profile_policy as p, profile_store as s
from tools.serve_recovery import test_profile_policy as fixtures

ROOT = Path(__file__).resolve().parents[2]


class IoTests(unittest.TestCase):
    def setUp(self):
        base = ROOT/'.q4t-work/serve-adaptive-profiles-20261111/io-fixtures'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.binding = access_store.binding(self.path, str(uuid.uuid4()))
        self.stamp = ledger.Stamp(str(uuid.uuid4()), time.monotonic_ns())
        s.initialize_bound(self.binding, p.initial(fixtures.registry(), self.stamp))

    def close(self, worker):
        end = time.monotonic()+3
        while not worker.close() and time.monotonic() < end: time.sleep(.01)
        self.assertIsNone(worker.process)
        self.assertIsNone(worker.claim)

    def worker(self):
        worker = profile_io.Worker(self.binding)
        self.addCleanup(self.close, worker)
        return worker

    def until(self, worker, predicate, seconds=4):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            worker.step()
            if predicate(): return
            time.sleep(.01)
        self.fail('profile worker deadline: '+str(worker.status()))

    def test_actual_worker_commits_and_replays_after_process_replacement(self):
        worker = self.worker(); self.until(worker, worker.applied)
        previous = worker.value
        candidate = p.prepare(previous, 'light', 'cold_memory_capacity',
                              str(uuid.uuid4()), 0, self.stamp)
        worker.commit(previous, candidate)
        self.assertFalse(worker.applied()); self.until(worker, worker.applied)
        first = worker.process.pid
        worker.process.kill()
        self.until(worker, lambda: worker.process is None)
        self.until(worker, worker.applied)
        self.assertNotEqual(worker.process.pid, first)
        self.assertEqual(worker.value, candidate)
        self.assertEqual(len(worker.value['history']), 1)
        self.assertEqual((worker.started, worker.reaped), (2, 1))
        self.assertFalse(worker.status()['model_actions'])

    def test_stalled_worker_cleanup_cannot_signal_healthy_process_or_erase_intent(self):
        sentinel = subprocess.Popen([sys.executable, '-I', '-c',
                                     'import time; time.sleep(30)'])
        def close_sentinel():
            if sentinel.poll() is None: sentinel.terminate()
            sentinel.wait(timeout=3)
        self.addCleanup(close_sentinel)
        worker = self.worker(); self.until(worker, worker.applied)
        previous = worker.value
        candidate = p.prepare(previous, 'light', 'cold_memory_capacity',
                              str(uuid.uuid4()), 0, self.stamp)
        worker.commit(previous, candidate); self.until(worker, worker.applied)
        child = worker.process
        os.kill(child.pid, signal.SIGSTOP); worker.next_read = 0
        self.until(worker, lambda: worker.error == 'reader_timeout')
        self.assertTrue(worker.status()['worker_unreaped'])
        self.assertFalse(worker.applied()); self.assertIsNone(sentinel.poll())
        self.until(worker, lambda: worker.process is None)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.until(worker, worker.applied)
        self.assertEqual(worker.value, candidate)
        self.assertIsNone(sentinel.poll())

    def test_invalid_storage_freezes_new_switches_and_does_not_initialize(self):
        worker = self.worker(); self.until(worker, worker.applied)
        pid = worker.process.pid
        with s.open_bound(self.binding):
            worker.reload(); self.until(worker, lambda: worker.error == 'writer_busy')
            self.assertFalse(worker.applied())
        path = self.path/'profile-selection/policy.json'
        saved = path.read_bytes(); path.write_bytes(b'{')
        worker.reload(); self.until(worker, lambda: worker.error == 'state_invalid')
        self.assertFalse(worker.applied())
        path.write_bytes(saved)
        worker.reload(); self.until(worker, worker.applied)
        self.assertEqual(worker.process.pid, pid)
        path.unlink()
        worker.reload(); self.until(worker, lambda: worker.error == 'state_unavailable')
        self.assertFalse(worker.applied()); self.assertFalse(path.exists())
        self.assertEqual(worker.started, 1)


if __name__ == '__main__': unittest.main()
