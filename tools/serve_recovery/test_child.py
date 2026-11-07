"""CPU negative controls for the model exec barrier and its borrowed lease."""
import fcntl
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import child, control, journal, ledger


class ChildTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR', ROOT/'build/recovery-child'))
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.path = Path(self.temp.name)/'private-cpu-lease'
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def tearDown(self):
        os.close(self.fd)
        self.temp.cleanup()

    def test_unlocked_shared_description_is_not_reacquired(self):
        borrowed = os.dup(self.fd)
        try:
            child.verify_lease(borrowed, self.path)
            fcntl.flock(borrowed, fcntl.LOCK_UN)  # Intentional private negative control.
            with self.assertRaises(ledger.LedgerError):
                child.verify_lease(self.fd, self.path)
            with self.path.open('r+b') as probe:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(borrowed)

    def test_separate_description_cannot_claim_held_lease(self):
        with self.path.open('r+b') as other:
            with self.assertRaises(ledger.LedgerError):
                child.verify_lease(other.fileno(), self.path)
        child.verify_lease(self.fd, self.path)

    def test_replaced_path_cannot_transfer_lease(self):
        self.path.rename(self.path.with_suffix('.old'))
        self.path.write_bytes(b'')
        with self.assertRaises(ledger.LedgerError):
            child.verify_lease(self.fd, self.path)

    def barrier(self, permit):
        marker = self.path.parent/'unexpected-exec'
        parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with (self.path.parent/'child.log').open('wb') as log:
            process = subprocess.Popen([sys.executable, '-I', '-B',
                str(ROOT/'tools/serve_recovery/child.py'), '--fd', str(endpoint.fileno()),
                '--parent-pid', str(os.getpid()), '--parent-start',
                str(journal.process_start(os.getpid())), '--lease-fd', str(self.fd),
                '--lease-path', str(self.path), '--controller-generation', '3', '--',
                sys.executable, '-c', 'from pathlib import Path; import sys; '
                'Path(sys.argv[1]).write_text("executed")', str(marker)],
                pass_fds=(endpoint.fileno(), self.fd), stdout=log, stderr=log)
            endpoint.close()
            try:
                parent.settimeout(3)
                hello = journal.receive(parent)
                self.assertEqual(hello['pid'], process.pid)
                self.assertFalse(marker.exists())
                if permit is not None:
                    journal.send(parent, permit)
                parent.close()
                self.assertNotEqual(process.wait(timeout=3), 0)
                self.assertFalse(marker.exists())
                child.verify_lease(self.fd, self.path)
            finally:
                parent.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)

    def test_lost_permit_channel_never_executes_target(self):
        self.barrier(None)

    def test_stale_controller_permit_never_executes_target(self):
        self.barrier(dict(version=1, ticket=str(uuid.uuid4()),
                          controller_generation=2, model_generation=1))


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        # The real parent/credential handshake is exercised by the lifecycle
        # suite. Inject receipts into an already-bound private CPU channel.
        self.channel = control.ControllerChannel.__new__(control.ControllerChannel)
        self.channel.sock, self.peer = socket.socketpair(socket.AF_UNIX,
                                                       socket.SOCK_SEQPACKET)
        self.channel.sock.setblocking(False)
        self.channel.service_id = str(uuid.uuid4())
        self.channel.generation = 1
        self.channel.pending = 1
        self.receipt = dict(version=1, service_id=self.channel.service_id,
            generation=1, reply_to=1, ok=True, result=dict(allowed=True))

    def tearDown(self):
        self.channel.close()
        self.peer.close()

    def test_stale_generation_does_not_consume_pending_authority(self):
        journal.send(self.peer, dict(self.receipt, generation=2))
        with self.assertRaises(ledger.LedgerError):
            self.channel.poll()
        self.assertEqual(self.channel.pending, 1)

    def test_boolean_or_float_is_not_a_generation_or_sequence(self):
        for field in ('generation', 'reply_to'):
            for value in (True, 1.0):
                with self.subTest(field=field, value=value):
                    journal.send(self.peer, dict(self.receipt, **{field: value}))
                    with self.assertRaises(ledger.LedgerError):
                        self.channel.poll()
                    self.assertEqual(self.channel.pending, 1)

    def test_duplicate_receipt_cannot_be_consumed_twice(self):
        journal.send(self.peer, self.receipt)
        self.assertTrue(self.channel.poll()['ok'])
        journal.send(self.peer, self.receipt)
        with self.assertRaises(ledger.LedgerError):
            self.channel.poll()
        self.assertIsNone(self.channel.pending)

    def test_closed_channel_is_not_an_empty_success_receipt(self):
        self.peer.close()
        with self.assertRaises(ledger.LedgerError):
            self.channel.poll()


if __name__ == '__main__':
    unittest.main()
