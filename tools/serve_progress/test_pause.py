"""Real CPU-only ownership and single-thread pause negative controls."""
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_progress import pause


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.reader, self.writer = os.pipe()
        code = '''import os,sys,threading,time
def worker():
    while True:
        os.write(1,b'worker-running\\n')
        time.sleep(.02)
threading.Thread(target=worker,daemon=True).start()
os.write(1,b'main-ready\\n')
os.read(int(sys.argv[1]),1)
os.write(1,b'main-resumed\\n')
'''
        self.child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code,
            str(self.reader)], pass_fds=(self.reader,),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.wait_bytes(b'main-ready')
        self.owner = pause.identity(self.child.pid)

    def tearDown(self):
        os.close(self.reader)
        os.close(self.writer)
        if self.child.poll() is None:
            self.child.kill()
        self.child.communicate(timeout=3)

    def wait_bytes(self, expected):
        end = time.monotonic()+3
        data = b''
        while time.monotonic() < end:
            readable, _, _ = select.select([self.child.stdout], [], [], .1)
            if readable:
                part = os.read(self.child.stdout.fileno(), 4096)
                if not part:
                    break
                data += part
                self.assertLess(len(data), 65536)
                if expected in data:
                    return data
        self.fail('bounded CPU output missing '+repr(expected))

    def test_only_owned_main_thread_pauses_while_other_thread_runs(self):
        with pause.PausedMain(self.owner) as paused:
            self.assertTrue(paused.stopped)
            state = Path(f'/proc/{self.child.pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
            self.assertEqual(state, 't')
            self.wait_bytes(b'worker-running')
            self.wait_bytes(b'worker-running')
            self.wait_bytes(b'worker-running')
            os.write(self.writer, b'x')
            self.wait_bytes(b'worker-running')
        self.wait_bytes(b'main-resumed')
        self.assertEqual(self.child.wait(timeout=3), 0)

    def test_stale_birth_and_non_descendant_are_rejected_before_ptrace(self):
        with mock.patch.object(pause, 'trace') as trace:
            with self.assertRaisesRegex(ValueError, 'identity'):
                with pause.PausedMain(dict(self.owner, start_ticks=self.owner['start_ticks']+1)):
                    self.fail('stale identity accepted')
            with self.assertRaisesRegex(ValueError, 'descendant'):
                with pause.PausedMain(pause.identity(os.getpid())):
                    self.fail('self/non-descendant accepted')
            trace.assert_not_called()

    def test_exception_always_detaches_owned_main(self):
        paused = pause.PausedMain(self.owner)
        with self.assertRaisesRegex(RuntimeError, 'owned test failure'):
            with paused:
                raise RuntimeError('owned test failure')
        self.assertFalse(paused.attached)
        os.write(self.writer, b'x')
        self.wait_bytes(b'main-resumed')
        self.assertEqual(self.child.wait(timeout=3), 0)


if __name__ == '__main__':
    unittest.main()
