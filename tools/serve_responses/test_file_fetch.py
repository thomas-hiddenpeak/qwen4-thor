"""Actual isolated native HTTPS acquisition, with a local owned TLS server."""
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = b"Native file input TLS evidence.\n"


class Server:
    def __init__(self, certificate, key, response=None, handshake=True):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(0.05)
        self.port = self.listener.getsockname()[1]
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certificate, key)
        self.stop = threading.Event()
        self.handshake = handshake
        self.requests = []
        self.connections = 0
        self.response = response or (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
            + str(len(PAYLOAD)).encode() + b"\r\n\r\n" + PAYLOAD)
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            self.connections += 1
            connection.settimeout(0.5)
            try:
                if not self.handshake:
                    self.stop.wait(0.5)
                    continue
                with self.context.wrap_socket(connection, server_side=True) as tls:
                    request = b""
                    while b"\r\n\r\n" not in request and len(request) < 32768:
                        part = tls.recv(4096)
                        if not part:
                            break
                        request += part
                    if b"\r\n\r\n" in request:
                        self.requests.append(request)
                        tls.sendall(self.response)
            except (ssl.SSLError, TimeoutError, ConnectionError):
                pass  # Expected negative TLS fixtures are not serving successes.
            finally:
                connection.close()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.listener.close()
        if self.thread.is_alive():
            raise RuntimeError("owned TLS fixture did not terminate")


class NativeFileFetchTls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ["Q4T_FILE_FETCH_TEST_BIN"]).resolve()
        output = Path(os.environ["Q4T_TEST_OUTPUT_DIR"]).resolve()
        output.mkdir(parents=True, exist_ok=True)
        cls.work = Path(tempfile.mkdtemp(prefix="file-fetch-tls-", dir=output))
        cls.cert, cls.key = cls.work / "ca.pem", cls.work / "key.pem"
        command = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                   "-keyout", str(cls.key), "-out", str(cls.cert), "-days", "1",
                   "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"]
        done = subprocess.run(command, capture_output=True, timeout=15)
        (cls.work / "certificate.stdout").write_bytes(done.stdout)
        (cls.work / "certificate.stderr").write_bytes(done.stderr)
        if done.returncode:
            raise RuntimeError("local TLS certificate generation failed")

    def server(self, **kwargs):
        value = Server(self.cert, self.key, **kwargs)
        self.addCleanup(value.close)
        return value

    def fetch(self, server, name, host="localhost", **overrides):
        policy = dict(public_networks=False, allow_http=True,
                      allowed_networks=["127.0.0.1/32", "::1/128"],
                      ports=[server.port], ca_file=str(self.cert), timeout_ms=2000)
        policy.update(overrides)
        command = [str(self.binary), "--file-fetch-fixture",
                   f"https://{host}:{server.port}/document.txt", json.dumps(policy)]
        done = subprocess.run(command, capture_output=True, timeout=12)
        prefix = self.work / name
        prefix.with_suffix(".stdout").write_bytes(done.stdout)
        prefix.with_suffix(".stderr").write_bytes(done.stderr)
        self.assertEqual(done.returncode, 0, done.stderr.decode(errors="replace"))
        result = json.loads(done.stdout)
        self.assertEqual((result["started"], result["reaped"], result["active"]), (1, 1, 0))
        return result

    def test_valid_chain_and_hostname_deliver_original_file(self):
        server = self.server()
        result = self.fetch(server, "valid")
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["bytes"], len(PAYLOAD))
        self.assertEqual(result["sha256"], hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(result["mime_type"], "text/plain")
        self.assertEqual(len(server.requests), 1)
        self.assertNotIn(b"Authorization:", server.requests[0])

    def test_wrong_name_untrusted_and_missing_ca_do_not_send_http(self):
        for name, kwargs in [("wrong-name", {"host": "127.0.0.1"}),
                             ("untrusted", {"ca_file": ""}),
                             ("missing-ca", {"ca_file": str(self.work / "absent.pem")})]:
            with self.subTest(name=name):
                server = self.server()
                result = self.fetch(server, name, **kwargs)
                self.assertEqual((result["status"], result["code"]), (502, "file_fetch_tls_failed"))
                self.assertEqual((result["bytes"], result["sha256"]), (0, ""))
                self.assertEqual(len(server.requests), 0)

    def test_verified_https_redirect_cannot_downgrade_to_plaintext(self):
        server = self.server(response=b"HTTP/1.1 302 Move\r\nLocation: http://localhost/no\r\nContent-Length: 0\r\n\r\n")
        result = self.fetch(server, "downgrade")
        self.assertEqual((result["status"], result["code"]), (502, "file_fetch_redirect_invalid"))
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(result["bytes"], 0)

    def test_stalled_tls_handshake_has_bounded_owned_cleanup(self):
        server = self.server(handshake=False)
        result = self.fetch(server, "handshake-timeout", timeout_ms=120)
        self.assertEqual(result["status"], 408)
        self.assertEqual(result["bytes"], 0)
        self.assertEqual(len(server.requests), 0)


if __name__ == "__main__":
    unittest.main()
