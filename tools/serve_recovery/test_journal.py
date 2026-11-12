"""Real CPU journal process tests; never touch GPU, NVML or live services."""
import ctypes
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import journal, ledger, store


class JournalTests(unittest.TestCase):
    def setUp(self):
        output = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',
                                    ROOT/'build/recovery-journal-tests'))
        output.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=output)
        self.directory = Path(self.temp.name)
        self.service_id = str(uuid.uuid4())
        with store.Store(self.directory, self.service_id, initialize=True):
            pass
        self.children = []
        self.sockets = []
        self.log = (self.directory/'journal.log').open('wb')

    def tearDown(self):
        for sock in self.sockets:
            sock.close()
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
        self.log.close()
        self.temp.cleanup()

    def launch(self, *, service_id=None, sock_type=socket.SOCK_SEQPACKET):
        parent, child = socket.socketpair(socket.AF_UNIX, sock_type)
        self.sockets.append(parent)
        parent.settimeout(3)
        argv = [sys.executable, '-I', '-B', str(ROOT/
            'tools/serve_recovery/journal.py'), '--fd', str(child.fileno()),
            '--parent-pid', str(os.getpid()), '--parent-start',
            str(journal.process_start(os.getpid())), '--directory',
            str(self.directory), '--service-id', service_id or self.service_id]
        process = subprocess.Popen(argv, pass_fds=(child.fileno(),),
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log)
        child.close()
        self.children.append(process)
        return parent, process

    def packet(self, sequence, revision, operation, **args):
        return dict(version=1, sequence=sequence, request=dict(
            id=str(uuid.uuid4()), expected_revision=revision,
            operation=operation, args=args))

    def test_live_protocol_commits_before_reply_and_preserves_stop(self):
        sock, process = self.launch()
        ready = journal.receive(sock)
        self.assertEqual(ready['snapshot']['desired'], 'STOPPED')
        journal.send(sock, self.packet(1, 0, 'desired', state='RUNNING'))
        self.assertEqual(journal.receive(sock)['receipt']['revision'], 1)
        journal.send(sock, self.packet(2, 1, 'desired', state='STOPPED'))
        reply = journal.receive(sock)
        self.assertTrue(reply['ok'])
        sock.close()
        self.assertEqual(process.wait(timeout=3), 0)
        with store.Store(self.directory, self.service_id) as writer:
            self.assertEqual(writer.state['desired'], 'STOPPED')
            self.assertEqual(writer.state['revision'], 2)

    def test_real_worker_excludes_another_writer(self):
        sock, _process = self.launch()
        self.assertTrue(journal.receive(sock)['ok'])
        with self.assertRaises(BlockingIOError):
            store.Store(self.directory, self.service_id)

    def test_duplicate_wire_sequence_freezes_without_second_transaction(self):
        sock, process = self.launch()
        journal.receive(sock)
        packet = self.packet(1, 0, 'desired', state='RUNNING')
        journal.send(sock, packet)
        self.assertTrue(journal.receive(sock)['ok'])
        journal.send(sock, packet)
        self.assertTrue(journal.receive(sock)['recovery_frozen'])
        self.assertEqual(process.wait(timeout=3), 1)
        with store.Store(self.directory, self.service_id) as writer:
            self.assertEqual(writer.state['revision'], 1)

    def test_oversized_frame_is_rejected_not_parsed_as_prefix(self):
        sock, process = self.launch()
        journal.receive(sock)
        sock.send(b' '*journal.MAX_FRAME+b'{}')
        self.assertTrue(journal.receive(sock)['recovery_frozen'])
        self.assertEqual(process.wait(timeout=3), 1)
        with store.Store(self.directory, self.service_id) as writer:
            self.assertEqual(writer.state['revision'], 0)

    def test_corrupt_store_fails_closed_before_ready(self):
        (self.directory/store.STATE).write_bytes(b'{}')
        sock, process = self.launch()
        reply = journal.receive(sock)
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['code'], 'journal_contract')
        self.assertEqual(process.wait(timeout=3), 1)

    def test_wrong_persistent_identity_fails_closed(self):
        sock, process = self.launch(service_id=str(uuid.uuid4()))
        self.assertFalse(journal.receive(sock)['ok'])
        self.assertEqual(process.wait(timeout=3), 1)

    def test_stream_socket_cannot_impersonate_message_protocol(self):
        sock, process = self.launch(sock_type=socket.SOCK_STREAM)
        self.assertEqual(sock.recv(1), b'')
        self.assertNotEqual(process.wait(timeout=3), 0)

    def test_worker_death_after_commit_does_not_refund_authorized_slot(self):
        sock, process = self.launch()
        journal.receive(sock)
        journal.send(sock, self.packet(1, 0, 'desired', state='RUNNING'))
        journal.receive(sock)
        journal.send(sock, self.packet(2, 1, 'authorize', role='controller',
                                      proof={name: True for name in ledger.PROOFS}))
        receipt = journal.receive(sock)['receipt']
        process.kill()
        process.wait(timeout=3)
        replacement, _replacement_process = self.launch()
        state = journal.receive(replacement)['snapshot']
        self.assertEqual(state['active']['controller']['ticket'],
                         receipt['result']['grant']['ticket'])
        journal.send(replacement, self.packet(1, state['revision'], 'authorize',
            role='controller', proof={name: True for name in ledger.PROOFS}))
        reply = journal.receive(replacement)
        self.assertEqual(reply['receipt']['result']['reason'],
                         'unreconciled_grant')

    def test_dead_peer_does_not_leave_writer_running(self):
        sock, process = self.launch()
        journal.receive(sock)
        sock.close()
        self.assertEqual(process.wait(timeout=3), 0)
        with store.Store(self.directory, self.service_id):
            pass

    def test_parent_death_signal_cleans_suspended_journal_without_restart(self):
        # Act as a temporary subreaper so this test can prove the grandchild
        # actually exited, even on a host whose PID 1 does not reap promptly.
        libc = ctypes.CDLL(None, use_errno=True)
        old = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(old), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        code = '''
import os, signal, socket, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools.serve_recovery import journal
parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
command = [sys.executable, '-I', '-B', sys.argv[1] +
    '/tools/serve_recovery/journal.py', '--fd', str(child.fileno()),
    '--parent-pid', str(os.getpid()), '--parent-start',
    str(journal.process_start(os.getpid())), '--directory', sys.argv[2],
    '--service-id', sys.argv[3]]
p = subprocess.Popen(command, pass_fds=(child.fileno(),),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
child.close()
assert journal.receive(parent)['ok']
os.kill(p.pid, signal.SIGSTOP)
# Confirm stopped before removing the parent; SIGTERM must remain pending.
os.waitpid(p.pid, os.WUNTRACED)
print(p.pid, flush=True)
sys.stdin.buffer.read(1)
os._exit(0)
'''
        helper = None
        journal_pid = None
        reaped = False
        try:
            helper = subprocess.Popen([sys.executable, '-I', '-B', '-c', code,
                str(ROOT), str(self.directory), self.service_id],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log)
            self.children.append(helper)
            import select
            self.assertTrue(select.select([helper.stdout], [], [], 5)[0])
            journal_pid = int(helper.stdout.readline())
            helper.stdin.write(b'x')
            helper.stdin.flush()
            helper.wait(timeout=3)
            os.kill(journal_pid, signal.SIGCONT)
            deadline = time.monotonic()+3
            while time.monotonic() < deadline:
                pid, status = os.waitpid(journal_pid, os.WNOHANG)
                if pid:
                    reaped = True
                    self.assertTrue(os.WIFSIGNALED(status))
                    self.assertEqual(os.WTERMSIG(status), signal.SIGTERM)
                    break
                time.sleep(0.01)
            self.assertTrue(reaped, 'journal survived parent death')
            with store.Store(self.directory, self.service_id):
                pass
        finally:
            if helper is not None:
                if helper.poll() is None:
                    helper.kill()
                    helper.wait(timeout=3)
                helper.stdin.close()
                helper.stdout.close()
            if journal_pid is not None and not reaped:
                try:
                    os.kill(journal_pid, signal.SIGKILL)
                    os.waitpid(journal_pid, 0)
                except ProcessLookupError:
                    pass
            libc.prctl(36, old.value, 0, 0, 0)


if __name__ == '__main__':
    unittest.main()
