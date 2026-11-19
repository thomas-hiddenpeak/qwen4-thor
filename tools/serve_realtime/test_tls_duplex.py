"""Actual native TLS duplex over inherited sockets; no HTTP/model/GPU fixture."""
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
import unittest


class Peer:
    def __init__(self, owner, *, trusted=True, version=ssl.TLSVersion.TLSv1_3,
                 stall_ms=1000):
        self.output = Path(tempfile.mkdtemp(prefix='peer-', dir=owner.output))
        self.raw, child = socket.socketpair()
        self.backend, upstream = socket.socketpair()
        for sock in (self.raw, child, self.backend, upstream):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        self.raw.settimeout(2)
        self.backend.settimeout(2)
        child.setblocking(False)
        upstream.setblocking(False)
        self.process = subprocess.Popen([str(owner.binary), '--tls-peer',
            str(owner.cert), str(owner.key), str(child.fileno()),
            str(upstream.fileno()), str(stall_ms)],
            pass_fds=(child.fileno(), upstream.fileno()),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child.close()
        upstream.close()
        context = ssl.create_default_context(cafile=str(owner.cert) if trusted else None)
        context.minimum_version = context.maximum_version = version
        context.set_alpn_protocols(['http/1.1'])
        self.incoming, self.outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.tls = context.wrap_bio(self.incoming, self.outgoing,
                                   server_hostname='127.0.0.1')
        self.finished = False

    def flush(self):
        pending = self.outgoing.read()
        if pending:
            self.raw.sendall(pending)

    def handshake(self):
        while True:
            try:
                self.tls.do_handshake()
                self.flush()
                return
            except ssl.SSLWantReadError:
                self.flush()
                data = self.raw.recv(65536)
                if not data:
                    raise EOFError('handshake peer closed')
                self.incoming.write(data)

    def encoded(self, data):
        assert self.tls.write(data) == len(data)
        return self.outgoing.read()

    def read(self, size):
        output = bytearray()
        while len(output) < size:
            try:
                part = self.tls.read(size - len(output))
                if not part:
                    break
                output.extend(part)
            except ssl.SSLWantReadError:
                self.flush()
                data = self.raw.recv(65536)
                if not data:
                    self.incoming.write_eof()
                else:
                    self.incoming.write(data)
        return bytes(output)

    def result(self):
        out, err = self.process.communicate(timeout=4)
        self.finished = True
        (self.output/'stdout.txt').write_bytes(out)
        (self.output/'stderr.txt').write_bytes(err)
        if self.process.returncode:
            raise AssertionError(f'native peer exit {self.process.returncode}')
        return json.loads(out)

    def close(self):
        self.raw.close()
        self.backend.close()
        if not self.finished:
            try:
                out, err = self.process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                out, err = self.process.communicate(timeout=2)
            (self.output/'stdout.txt').write_bytes(out)
            (self.output/'stderr.txt').write_bytes(err)


def exact(sock, count):
    result = bytearray()
    while len(result) < count:
        part = sock.recv(count - len(result))
        if not part:
            break
        result.extend(part)
    return bytes(result)


class NativeTlsDuplex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ['Q4T_DUPLEX_TESTS']).resolve(strict=True)
        parent = Path(os.environ['Q4T_TEST_OUTPUT_DIR']).resolve(strict=True)
        cls.output = Path(tempfile.mkdtemp(prefix='tls-duplex-', dir=parent))
        cls.cert, cls.key = cls.output/'certificate.pem', cls.output/'key.pem'
        with (cls.output/'certificate.log').open('wb') as log:
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec',
                '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-days', '2',
                '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                '-addext', 'basicConstraints=critical,CA:FALSE', '-addext',
                'keyUsage=critical,digitalSignature', '-addext',
                'extendedKeyUsage=serverAuth', '-keyout', str(cls.key),
                '-out', str(cls.cert)], check=True, timeout=15,
                stdout=log, stderr=log)
        cls.key.chmod(0o600)

    def peer(self, **kwargs):
        peer = Peer(self, **kwargs)
        self.addCleanup(peer.close)
        return peer

    def test_partial_tls12_and_tls13_record_does_not_block_output(self):
        for version in (ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3):
            with self.subTest(version=version):
                peer = self.peer(version=version)
                peer.handshake()
                packet = peer.encoded(b'late-input')
                peer.raw.sendall(packet[:3])
                # A complete TLS header/payload is deliberately unavailable.
                peer.backend.sendall(b'early-output')
                self.assertEqual(peer.read(12), b'early-output')
                peer.raw.sendall(packet[3:])
                self.assertEqual(exact(peer.backend, 10), b'late-input')
                peer.backend.shutdown(socket.SHUT_WR)
                result = peer.result()
                self.assertEqual(result['reason'], 1)  # upstream EOF, not WS close
                self.assertEqual((result['client_sent'], result['upstream_sent']),
                                 (12, 10))

    def test_partial_writes_preserve_full_encrypted_payload_and_final_bytes(self):
        peer = self.peer()
        peer.handshake()
        payload = bytes(range(256)) * 2048
        def send():
            peer.backend.sendall(payload)
            peer.backend.shutdown(socket.SHUT_WR)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(send)
            time.sleep(0.04)  # tiny kernel buffers force SSL_write WANT_WRITE
            self.assertEqual(peer.read(len(payload)), payload)
            writer.result(timeout=3)
        result = peer.result()
        self.assertEqual(result['reason'], 1)
        self.assertEqual(result['client_sent'], len(payload))
        self.assertLessEqual(result['peak_to_client'], 65536)

    def test_blocked_tls_consumer_still_delivers_cancel_then_hits_write_deadline(self):
        peer = self.peer(stall_ms=300)
        peer.handshake()
        peer.backend.setblocking(False)
        total = 0
        until = time.monotonic() + 0.12
        while time.monotonic() < until:
            try:
                total += peer.backend.send(b'x' * 4096)
            except BlockingIOError:
                time.sleep(0.001)
        # TLS can block before one full 16-KiB record: the fixture's kernel
        # send buffers are deliberately only 4 KiB. Verify actual inability
        # to drain via the relay result below, not an assumed record count.
        self.assertGreater(total, 0)
        peer.raw.sendall(peer.encoded(b'cancel-now'))
        peer.backend.settimeout(2)
        self.assertEqual(exact(peer.backend, 10), b'cancel-now')
        result = peer.result()
        self.assertEqual(result['reason'], 3)  # independent write deadline
        self.assertEqual(result['upstream_sent'], 10)
        self.assertLess(result['client_sent'], total)
        self.assertLessEqual(result['peak_to_client'], 65536)

    def test_untrusted_tls_never_enters_duplex(self):
        peer = self.peer(trusted=False)
        with self.assertRaises(ssl.SSLCertVerificationError):
            peer.handshake()
        # Send the client's certificate alert, then close the TCP side.
        peer.flush()
        peer.raw.close()
        out, err = peer.process.communicate(timeout=3)
        peer.finished = True
        (peer.output/'stdout.txt').write_bytes(out)
        (peer.output/'stderr.txt').write_bytes(err)
        self.assertEqual(peer.process.returncode, 4)
        self.assertEqual(out, b'')
        self.assertEqual(peer.backend.recv(1), b'')


if __name__ == '__main__':
    unittest.main()
