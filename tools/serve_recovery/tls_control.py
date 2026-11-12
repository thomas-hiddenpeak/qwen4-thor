"""Native TLS validation and private reload; never signals or restarts a model."""
import hashlib
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time

from tools.serve_gateway import gateway as files
from tools.serve_recovery import journal, ledger


def settings(policy, tls, *, address, lease):
    return dict(version=2, parent_pid=os.getpid(),
        parent_start_ticks=journal.process_start(os.getpid()), address=address,
        policy=files.policy(policy), tls=tls, tls_lease=lease)


def check(binary, policy, tls, output):
    """Same native loader/policy as serving, in a bounded CPU child process.

    Runtime credentials deliberately live outside the immutable bundle. Their
    paths are sealed; their accepted certificate identity is reported by the
    live entrypoint. Atomic file replacement plus explicit reload persists over
    entrypoint restarts without modifying engine, package or recovery budgets.
    """
    identity = hashlib.sha256(ledger.canonical(tls)).hexdigest()[:32]
    with tempfile.TemporaryDirectory(prefix='tls-check-', dir=output) as temporary:
        path = Path(temporary)/'config.json'
        files.save(path, ledger.canonical(settings(policy, tls,
            address='q4t-tls-check-'+identity, lease='q4t-tls-check-'+identity)))
        result = subprocess.run([str(binary), '--check-tls', str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=5, close_fds=True, env={'PATH': '/usr/bin:/bin', 'LANG': 'C'})
        ledger.require(len(result.stdout) <= 4096 and result.returncode == 0,
                       'native TLS credential validation failed')
        status = files.parse(result.stdout)
        ledger.require(status.get('enabled') is True and status.get('ready') is True,
                       'native TLS credential validation failed')
        return status


def request(service_id, generation, operation, *, binary_sha256,
            parent_pid, parent_start):
    ledger.require(ledger.identifier(service_id) and
        ledger.integer(generation, minimum=1) and operation in ('status', 'reload'),
        'invalid TLS control request')
    address = f'\0q4t-ingress-{os.geteuid()}-{service_id}-{generation}-admin'
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
        sock.settimeout(1.5)
        sock.connect(address)
        pid, uid, _ = struct.unpack('3i', sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        ledger.require(uid == os.geteuid() and
            journal.process_start(parent_pid) == parent_start,
            'TLS control parent identity changed')
        start = journal.process_start(pid)
        pidfd = os.pidfd_open(pid)
        try:
            stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            ledger.require(int(stat[1]) == parent_pid and
                files.sha(Path(f'/proc/{pid}/exe')) == binary_sha256 and
                journal.process_start(pid) == start,
                'TLS control entrypoint identity changed')
            journal.send(sock, dict(op=operation))
            result = journal.receive(sock)
            ledger.require(type(result) is dict and type(result.get('enabled')) is bool,
                           'invalid TLS control reply')
            return dict(result, entrypoint_pid=pid, entrypoint_start_ticks=start)
        finally:
            os.close(pidfd)


def execute(descriptor, snapshot, manifest, *, reload=False):
    ledger.require(manifest.get('ingress_mode') == 'native' and
                   snapshot.get('gateway_pid') is not None,
                   'native entrypoint is not running')
    def call(operation):
        return request(descriptor['service_id'], snapshot['gateway_generation'],
            operation, binary_sha256=manifest['files']['bin/q4t-ingress'],
            parent_pid=snapshot['gateway_pid'], parent_start=snapshot['gateway_start_ticks'])
    result = call('reload' if reload else 'status')
    if not reload or not result.get('reload_accepted'):
        return result
    identity = (result['entrypoint_pid'], result['entrypoint_start_ticks'])
    attempt = result['load_attempt']
    deadline = time.monotonic()+3
    while result.get('reload_pending') and time.monotonic() < deadline:
        time.sleep(0.05)
        result = call('status')
        ledger.require((result['entrypoint_pid'], result['entrypoint_start_ticks']) == identity
                       and result['load_attempt'] == attempt,
                       'TLS reload ownership or attempt changed')
    return dict(result, reload_accepted=True,
                reloaded=not result.get('reload_pending') and not result.get('error')
                         and result.get('ready') is True)


class Observation:
    """Nonblocking private TLS observation, no /proc or executable file reads.

    A confirmed expired credential closes entrypoint admission, not the model.
    Missing observations yield None; they cannot turn a healthy core into a
    restart/stop decision. The actual handshake always checks expiry itself.
    """
    def __init__(self, address):
        self.address = '\0'+address+'-admin'
        self.socket = None
        self.deadline = self.retry_at = 0

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def step(self, child_pid):
        now = time.monotonic()
        try:
            if self.socket is None:
                if now < self.retry_at:
                    return None
                self.retry_at = now+0.5
                sock = self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                sock.setblocking(False)
                sock.connect(self.address)
                pid, uid, _ = struct.unpack('3i', sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                ledger.require(pid == child_pid and uid == os.geteuid(),
                               'TLS observation peer changed')
                journal.send(sock, dict(op='status'))
                self.deadline = now+0.5
            if now >= self.deadline:
                self.close()
                return None
            try:
                raw, _, flags, _ = self.socket.recvmsg(4096)
            except BlockingIOError:
                return None
            self.close()
            ledger.require(raw and not flags & socket.MSG_TRUNC, 'invalid TLS observation')
            result = files.parse(raw)
            ledger.require(result.get('enabled') is True and
                           type(result.get('ready')) is bool, 'invalid TLS observation')
            return result['ready']
        except (OSError, ValueError, RuntimeError, TypeError):
            self.close()
            return None
