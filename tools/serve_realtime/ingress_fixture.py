"""Private native TLS ingress for bounded verification, never a model launcher.

Uses the production CPU ingress and its PID-bound admission transport. A fixed
owned-backend decision is an explicit controller fixture, not Guardian recovery
qualification. API keys, TLS keys and binding tokens stay in private files.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

from tools.serve_gateway import gateway as files
from tools.serve_recovery import journal
from tools.serve_recovery.native_ingress import AdmissionSocket


def certificate(directory):
    directory.mkdir(mode=0o700)
    cert, key = directory/'certificate.pem', directory/'key.pem'
    with (directory/'openssl.log').open('xb') as log:
        subprocess.run(['openssl','req','-x509','-newkey','ec','-pkeyopt',
            'ec_paramgen_curve:P-256','-nodes','-days','2','-subj','/CN=127.0.0.1',
            '-addext','subjectAltName=IP:127.0.0.1','-addext','basicConstraints=critical,CA:FALSE',
            '-addext','extendedKeyUsage=serverAuth','-keyout',str(key),'-out',str(cert)],
            check=True, timeout=15, stdout=log, stderr=log)
    cert.chmod(0o644);key.chmod(0o600)
    return dict(certificate=str(cert), key=str(key), ca='', require_client_certificate=False)


class NativeIngress:
    def __init__(self, directory, binary, port, backend_port, operations_port,
                 token, clients, tls, backend_alive, *, client_inflight=2):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700)
        self.port = port
        self.process = None
        self.closed = threading.Event()
        self.errors = []
        address = 'q4t-realtime-test-'+os.urandom(12).hex()
        def decide(lane):
            if not backend_alive(): return dict(error='service_stopping')
            return dict(port=operations_port if lane == 'control' else backend_port, token=token)
        self.server = AdmissionSocket(address, clients, decide,
            lambda: self.process.pid if self.process is not None and self.process.poll() is None else None)
        policy = files.policy(dict(version=2, listen_host='127.0.0.1', listen_port=port,
            server_name='127.0.0.1', upstream_port=backend_port,
            upstream_operations_port=operations_port, allowed_client_cidrs=['127.0.0.1/32'],
            clients=[dict(id=name, api_key=key) for name,key in clients.items()],
            per_client_rps=20, burst=20, per_client_inflight=client_inflight, global_inflight=8,
            body_bytes=1048576, upstream_idle_seconds=180))
        config = dict(version=2, parent_pid=os.getpid(),
            parent_start_ticks=journal.process_start(os.getpid()), address=address,
            policy=policy, tls=tls, tls_lease=address+'-tls')
        self.path = self.directory/'ingress.json'
        self.path.write_text(json.dumps(config));self.path.chmod(0o600)
        self.log = (self.directory/'ingress.log').open('xb', buffering=0)
        self.thread = threading.Thread(target=self.serve, name='owned-admission')
        try:
            self.process = subprocess.Popen([str(binary), '--config', str(self.path)],
                stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log)
            self.thread.start()
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError('native ingress exited; inspect private ingress.log')
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.1):
                        break
                except OSError: time.sleep(.02)
            else: raise TimeoutError('native ingress listener')
        except BaseException:
            self.close();raise

    def serve(self):
        try:
            while not self.closed.is_set(): self.server.step(.02)
        except BaseException as error:
            self.errors.append(type(error).__name__)

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill();self.process.wait(3)
        self.closed.set()
        if self.thread.is_alive(): self.thread.join(2)
        if self.thread.is_alive(): raise RuntimeError('admission thread failed to stop')
        self.server.close();self.log.close()
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1',self.port))
        if self.errors: raise RuntimeError('admission loop failed')
