"""Independent bounded metrics exporter; failures cannot signal the service."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_host import native
from tools.serve_recovery import journal, ledger, store


class Collector:
    """One collector, including an unreaped child, and one fixed controller thread."""
    def __init__(self, package, digest, state):
        self.args = (str(package), digest, str(state))
        self.stop = threading.Event()
        self.value = None
        self.reason = 'collector_unavailable'
        self.child = None
        self.starts = []
        self.started = self.reaped = 0
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        lock = channel = child_end = None
        try:
            # The child also holds this claim. A stuck orphan prevents a second
            # exporter from multiplying collectors for the same service state.
            lock = os.open(Path(self.args[2]) / 'native-observer.lock',
                os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            info = os.fstat(lock)
            ledger.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
                'private observer claim')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while not self.stop.is_set():
                now = time.monotonic()
                self.starts = [t for t in self.starts if now - t < 60]
                if len(self.starts) >= 3:
                    self.reason = 'collector_budget'
                    self.stop.wait(.1)
                    continue
                self.starts.append(now)  # Reserve before spawn, including failures.
                channel, child_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                channel.setblocking(False)
                parent_start = journal.process_start(os.getpid())
                args = [sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                    '--worker', '--fd', str(child_end.fileno()), '--parent', str(os.getpid()),
                    '--parent-start', str(parent_start), '--package', self.args[0],
                    '--digest', self.args[1], '--state', self.args[2]]
                self.value = None
                try:
                    self.child = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                        close_fds=True, pass_fds=(child_end.fileno(), lock))
                    self.started += 1
                    child_end.close(); child_end = None
                    last, sequence = time.monotonic(), 0
                    while not self.stop.is_set() and self.child.poll() is None:
                        try:
                            raw, ancillary, flags, _ = channel.recvmsg(native.MAX_PACKET)
                        except BlockingIOError:
                            raw = None
                        if raw is not None:
                            ledger.require(raw and not ancillary and not flags &
                                (socket.MSG_TRUNC | socket.MSG_CTRUNC), 'collector frame')
                            message = store.decode(raw)
                            ledger.keys(message, ('sequence', 'value'))
                            ledger.require(type(message['sequence']) is int and
                                message['sequence'] == sequence + 1, 'collector sequence')
                            sequence += 1
                            value = message['value']
                            ledger.require(type(value) is dict and value['version'] == 1
                                and value['state'] in native.STATES and
                                value['reason'] in native.REASONS, 'collector projection')
                            # Exercise the fixed public projection before publishing.
                            ledger.require(len(native.metrics(value)) <= native.MAX_PACKET,
                                           'collector metric bound')
                            self.value, self.reason = value, 'collector_unavailable'
                            last = time.monotonic()
                        if time.monotonic() - last >= 4:
                            self.reason = 'collector_timeout'
                            break
                        self.stop.wait(.025)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    self.reason = 'collector_unavailable'
                finally:
                    # Only Popen's unreaped direct child is signalable here.
                    # Never use any PID read from a service/observation record.
                    self.value = None
                    if self.child is not None:
                        if self.child.poll() is None:
                            self.child.kill()
                        while self.child.poll() is None:
                            time.sleep(.05)  # Retain claim/capacity until actual reap.
                        self.reaped += 1
                        self.child = None
                    if child_end is not None:
                        child_end.close(); child_end = None
                    channel.close(); channel = None
                self.stop.wait(1)
        except (OSError, ValueError, RuntimeError):
            self.reason = 'collector_unavailable'
        finally:
            if channel is not None:
                channel.close()
            if child_end is not None:
                child_end.close()
            if lock is not None:
                os.close(lock)

    def close(self):
        self.stop.set()
        self.thread.join(.2)
        # A blocked kernel syscall remains owned, not replaced by a new thread.
        return not self.thread.is_alive()


def worker(args):
    with socket.socket(fileno=args.fd) as sock:
        journal.verify_parent(sock, args.parent, args.parent_start)
        sock.settimeout(1)
        binding = native.bind(args.package, args.digest, args.state)
        sequence = 0
        while True:
            value = native.collect(binding)
            sequence += 1
            raw = ledger.canonical(dict(sequence=sequence, value=value))
            ledger.require(len(raw) <= native.MAX_PACKET, 'collector frame bound')
            ledger.require(sock.send(raw) == len(raw), 'short collector frame')
            time.sleep(1)


def response(raw, value):
    status, body, content_type = 200, b'', 'application/json'
    head, separator, rest = raw.partition(b'\r\n\r\n')
    method = b''
    if len(head) > 8192:
        status = 431
    else:
        lines = head.split(b'\r\n')
        parts = lines[0].split(b' ')
        if len(parts) != 3 or parts[2] not in (b'HTTP/1.0', b'HTTP/1.1') or not separator:
            status = 400
        else:
            method, path, version = parts
            headers = {}
            for line in lines[1:]:
                name, sep, val = line.partition(b':')
                if not sep or not name or any(c not in b'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-' for c in name) or any(c < 32 or c == 127 for c in val):
                    status = 400; break
                name = name.lower()
                if name in headers:
                    status = 400; break
                headers[name] = val.strip()
            if (rest or headers.get(b'content-length', b'0') != b'0' or
                    b'transfer-encoding' in headers or b'expect' in headers or
                    version == b'HTTP/1.1' and not headers.get(b'host')):
                status = 400
            if status == 200:
                if method not in (b'GET', b'HEAD'):
                    status = 405
                elif b'?' in path or b'#' in path:
                    status = 400
                elif path == b'/metrics':
                    body, content_type = native.metrics(value).encode(), 'text/plain; version=0.0.4'
                elif path == b'/status':
                    body = ledger.canonical(value)
                else:
                    status = 404
    if status != 200:
        body = b'{"error":"observation_request_rejected"}\n'
    ledger.require(len(body) <= native.MAX_PACKET, 'public response bound')
    reason = {200: 'OK', 400: 'Bad Request', 404: 'Not Found',
              405: 'Method Not Allowed', 431: 'Request Header Fields Too Large'}[status]
    header = (f'HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\n'
        f'Content-Length: {len(body)}\r\nConnection: close\r\nCache-Control: no-store\r\n'
        'X-Content-Type-Options: nosniff\r\n' +
        ('Allow: GET, HEAD\r\n' if status == 405 else '') + '\r\n').encode()
    return header + (b'' if method == b'HEAD' else body)


def serve(listener, collector, stopping):
    """16 connections, 8 KiB headers, 128 KiB bodies, 2s total connection life."""
    boot = native.stamp()['boot_id']
    with selectors.DefaultSelector() as selector:
        listener.setblocking(False)
        selector.register(listener, selectors.EVENT_READ, None)
        clients = {}
        def close(sock):
            selector.unregister(sock)
            clients.pop(sock, None)
            sock.close()
        try:
            while not stopping.is_set():
                for key, events in selector.select(.05):
                    sock = key.fileobj
                    if sock is listener:
                        try:
                            conn, _ = listener.accept()
                        except BlockingIOError:
                            continue
                        if len(clients) >= 16:
                            conn.close(); continue
                        conn.setblocking(False)
                        clients[conn] = dict(until=time.monotonic() + 2, raw=bytearray(), output=None)
                        selector.register(conn, selectors.EVENT_READ, clients[conn])
                        continue
                    entry = key.data
                    try:
                        if events & selectors.EVENT_READ:
                            part = sock.recv(8193 - len(entry['raw']))
                            if not part:
                                close(sock); continue
                            entry['raw'].extend(part)
                            if b'\r\n\r\n' in entry['raw'] or len(entry['raw']) > 8192:
                                value = native.current(collector.value, now=dict(boot_id=boot,
                                    boottime=time.clock_gettime(time.CLOCK_BOOTTIME), unix=time.time()),
                                    reason=collector.reason)
                                entry['output'] = response(bytes(entry['raw']), value)
                                selector.modify(sock, selectors.EVENT_WRITE, entry)
                        elif events & selectors.EVENT_WRITE:
                            n = sock.send(entry['output'][:16384])
                            entry['output'] = entry['output'][n:]
                            if not n or not entry['output']:
                                close(sock)
                    except BlockingIOError:
                        pass
                    except (OSError, ValueError, KeyError, TypeError):
                        close(sock)
                now = time.monotonic()
                for sock, entry in list(clients.items()):
                    if now >= entry['until']:
                        close(sock)
        finally:
            for sock in list(clients):
                close(sock)


def export(package, digest, state, port):
    ledger.require(type(port) is int and 1024 <= port <= 65535, 'export port bound')
    stopping = threading.Event()
    previous = {s: signal.signal(s, lambda *_: stopping.set()) for s in (signal.SIGINT, signal.SIGTERM)}
    collector = None
    clean = True
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', port))
            listener.listen(16)
            collector = Collector(package, digest, state)
            serve(listener, collector, stopping)
    finally:
        if collector is not None:
            clean = collector.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0 if clean else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    for name in ('fd', 'parent', 'parent-start', 'port'):
        parser.add_argument('--' + name, type=int)
    for name in ('package', 'digest', 'state'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    if args.worker:
        return worker(args)
    return export(args.package, args.digest, args.state, args.port)


if __name__ == '__main__':
    raise SystemExit(main())
