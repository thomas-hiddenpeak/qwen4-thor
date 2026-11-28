"""Verify diagnostic backpressure, recovery and shutdown using Linux pipes."""
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DiagnosticSinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('g++-14') or shutil.which('c++')
        if not compiler:
            raise unittest.SkipTest('C++ compiler required')
        build = ROOT / 'build'
        build.mkdir(exist_ok=True)
        scratch = tempfile.TemporaryDirectory(dir=build)
        cls.addClassCleanup(scratch.cleanup)
        cls.probe = Path(scratch.name) / 'diagnostic_sink_probe'
        subprocess.run([
            compiler, '-std=c++23', '-pthread', '-Wall', '-Wextra', '-Werror',
            '-I', ROOT / 'include',
            ROOT / 'tools/serve_audit/diagnostic_sink_probe.cpp',
            ROOT / 'src/server/serve_diagnostics.cpp',
            ROOT / 'src/service/guardian_process.cpp', '-o', cls.probe,
        ], check=True)

    def channel(self, sink):
        process = subprocess.Popen([self.probe], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=sink, bufsize=0)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            process.stdin.close()
            process.stdout.close()
        self.addCleanup(cleanup)
        self.assertEqual(self.reply(process), b'ready')
        return process

    def reply(self, process):
        self.assertTrue(select.select([process.stdout], [], [], 3)[0],
                        'diagnostic output blocked forward progress')
        return process.stdout.readline().strip()

    def test_stalled_reader_then_resumed_and_closed(self):
        read_fd, write_fd = os.pipe()
        with os.fdopen(write_fd, 'wb', buffering=0) as writer:
            with os.fdopen(read_fd, 'rb', buffering=0) as reader:
                process = self.channel(writer)
                tasks = Path('/proc', str(process.pid), 'task')
                self.assertEqual(len(list(tasks.iterdir())), 2)
                process.stdin.write(b'burst\n')
                dropped = int(self.reply(process))
                self.assertGreater(dropped, 0)
                self.assertLess(dropped, 8192)
                self.assertTrue(os.get_blocking(writer.fileno()))
                self.assertEqual(len(list(tasks.iterdir())), 2)
                # Drain until the marker arrives. If the queue is still full,
                # an individual marker can be shed along with other diagnostics.
                deadline = time.monotonic() + 3
                tail = b''
                while b'diagnostic-after-resume\n' not in tail:
                    self.assertLess(time.monotonic(), deadline)
                    process.stdin.write(b'marker\n')
                    self.assertEqual(self.reply(process), b'wrote')
                    if select.select([reader], [], [], .1)[0]:
                        tail = tail[-32:] + reader.read(65536)
            process.stdin.write(b'marker\n')
            self.assertEqual(self.reply(process), b'wrote')
            process.stdin.write(b'done\n')
            self.assertEqual(process.wait(timeout=3), 0)

    def test_descriptor_exhaustion_keeps_stderr_out_of_application_files(self):
        reader, writer = socket.socketpair()
        with reader, writer:
            for name, stdin in (('readonly', subprocess.DEVNULL),
                                ('readwrite', reader.fileno())):
                with self.subTest(stdin=name):
                    destination = self.probe.parent / name
                    child = subprocess.run(
                        [self.probe, '--limited-fds', destination], stdin=stdin,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        check=True, timeout=3)
                    self.assertGreater(int(child.stdout), 2)
                    self.assertEqual(destination.read_bytes(), b'')

    def test_failed_sink_preserves_progress_and_exit(self):
        with open('/dev/full', 'wb', buffering=0) as sink:
            process = self.channel(sink)
            process.stdin.write(b'marker\nmarker\ndone\n')
            self.assertEqual(self.reply(process), b'wrote')
            self.assertEqual(self.reply(process), b'wrote')
            self.assertEqual(process.wait(timeout=3), 0)


if __name__ == '__main__':
    unittest.main()
