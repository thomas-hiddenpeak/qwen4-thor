"""Guardian-owned ingress; live per-request binding, no application replay."""
import argparse
import hashlib
import hmac
import itertools
import os
from pathlib import Path
import secrets
import select
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_gateway import gateway as legacy
from tools.serve_ops import identity
from tools.serve_recovery import binding, control, gateway_config, journal, ledger
from tools.serve_service import supervisor


def listener(pid, host, port, binary):
    """Verify an exact IPv4/IPv6 listener and its owning executable."""
    owner = supervisor.process_identity(pid)
    proc = Path('/proc')/str(pid)
    own, selected = Path('/proc/self/ns/net').stat(), (proc/'ns/net').stat()
    ledger.require((own.st_dev, own.st_ino) == (selected.st_dev, selected.st_ino),
                   'gateway network namespace differs')
    descriptors = list(itertools.islice((proc/'fd').iterdir(), 1025))
    ledger.require(len(descriptors) <= 1024, 'gateway descriptor observation bound')
    owned = set()
    for path in descriptors:
        try:
            owned.add(os.readlink(path))
        except FileNotFoundError:
            pass
    ipv6 = ':' in host
    packed = socket.inet_pton(socket.AF_INET6 if ipv6 else socket.AF_INET, host)
    address = b''.join(packed[i:i+4][::-1] for i in range(0, len(packed), 4)).hex().upper()
    matches = []
    for row in identity.bounded_file('/proc/net/tcp6' if ipv6 else '/proc/net/tcp').decode().splitlines()[1:]:
        fields = row.split()
        ledger.require(len(fields) >= 10, 'invalid gateway listener observation')
        if fields[1].upper() == f'{address}:{port:04X}' and fields[3] == '0A':
            matches.append(fields[9])
    ledger.require(len(matches) == 1 and 'socket:['+matches[0]+']' in owned,
                   'gateway listener not owned')
    digest = hashlib.sha256(identity.bounded_file(proc/'exe', 32*1024**2,
                                                follow=True)).hexdigest()
    ledger.require(digest == binary and supervisor.process_identity(pid) == owner,
                   'gateway executable changed')
    return dict(owner=owner, listener_inode=int(matches[0]), executable_sha256=digest)


def unavailable(snapshot=None):
    if snapshot is None:
        return 'service_binding_unavailable'
    if snapshot.get('stop_requested'):
        return 'service_stopping'
    if snapshot.get('state') == 'QUARANTINED':
        return 'service_quarantined'
    if snapshot.get('quota_wait_ms', 0) > 0:
        return 'service_quota_wait'
    return 'service_recovering'


class Admission:
    def __init__(self, config, channel, *, stopped=None):
        self.config, self.channel = gateway_config.validate(config), channel
        self.stopped = stopped
        self.cached = None
        self.pidfd = None
        self.retry_at = 0
        self.policy_channel = None
        self.verified_clients = ()

    def close(self):
        self.cached = None
        if self.policy_channel is not None:self.policy_channel.close()
        self.policy_channel = None
        self.verified_clients = ()
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd = None

    @staticmethod
    def key(snapshot):
        owner = snapshot['model_owner']
        ledger.keys(owner, ('pid', 'start_ticks', 'boot_id'))
        ledger.require(ledger.integer(owner['pid'], minimum=2) and
                       ledger.integer(owner['start_ticks'], minimum=1) and
                       ledger.identifier(owner['boot_id']) and
                       ledger.integer(snapshot['model_generation'], minimum=1),
                       'invalid model owner')
        return ledger.canonical(dict(owner=owner, binding=snapshot['binding'],
                                     generation=snapshot['model_generation']))

    def fresh(self):
        snapshot = control.binding(self.channel.service_id)
        ledger.require(all(type(snapshot[key]) is bool for key in
            ('model_ready', 'business_ready', 'stop_requested')),
            'invalid readiness projection')
        return snapshot

    def matches(self, snapshot):
        return (self.cached is not None and self.pidfd is not None and
                snapshot['model_ready'] and not snapshot['stop_requested'] and
                snapshot['binding'] is not None and
                not select.select([self.pidfd], [], [], 0)[0] and
                self.key(snapshot) == self.cached)

    def update(self):
        """Independent verification runs before opening a new generation."""
        try:
            snapshot = self.fresh()
            if self.matches(snapshot):
                installed = self.update_policy(snapshot)
                if installed and snapshot['gateway_bound_model'] != snapshot['model_generation']:
                    self.channel.bound(snapshot['model_generation'])
                return
            self.close()
            self.channel.unbound()
            if (not snapshot['model_ready'] or snapshot['stop_requested'] or
                    snapshot['binding'] is None or time.monotonic() < self.retry_at):
                return
            self.retry_at = time.monotonic()+1
            key = self.key(snapshot)
            p, value = self.config['policy'], snapshot['binding']
            expected = binding.validate(value, base=self.config['backend_policy'],
                port=p['upstream_port'], operations_port=p['upstream_operations_port'],
                binary=self.config['binary_sha256'], protocol=self.config['protocol_sha256'])
            owner = snapshot['model_owner']
            fd = os.pidfd_open(owner['pid'])
            try:
                ledger.require(supervisor.process_identity(owner['pid']) == owner,
                               'gateway model owner changed')
                observation = supervisor.probe(owner['pid'], value['port'],
                    value['operations_port'], stopped=self.stopped, include_metrics=False)
                result = supervisor.classify(observation, value['protocol_sha256'],
                    value['binary_sha256'], expected, value['operations_port'])
                ledger.require(result == 'READY', 'new backend is not ready')
                current = self.fresh()
                ledger.require(current['model_ready'] and not current['stop_requested'] and
                    self.key(current) == key and not select.select([fd], [], [], 0)[0],
                    'generation changed during gateway verification')
                self.cached, self.pidfd = key, fd
                fd = None
                if self.update_policy(snapshot):self.channel.bound(snapshot['model_generation'])
            finally:
                if fd is not None:
                    os.close(fd)
        except (OSError, ValueError, RuntimeError, KeyError):
            self.close()
            self.channel.unbound()

    def update_policy(self, snapshot):
        if self.config['version'] != 4:return True
        try:
            from tools.serve_recovery import quota_control
            if self.policy_channel is None:
                self.policy_channel = quota_control.Channel(quota_control.address(
                    self.channel.service_id, snapshot['model_generation']))
            owner = snapshot['model_owner']
            value = self.policy_channel.step(owner['pid'], owner['start_ticks'])
            observed = value['observed']
            if observed is not None and not value['error']:
                ids = tuple(observed['clients'])
                prefix = self.verified_clients or tuple(c['id'] for c in self.config['backend_policy']['clients'])
                ledger.require(observed['configured'] and observed['mutable'] and
                    len(ids) >= len(prefix) and ids[:len(prefix)] == prefix,
                    'model client slots changed')
                self.verified_clients = ids
        except Exception:
            pass
        # Installed slots cannot be removed/reused within this model. A lost
        # optional observation retains known clients; a new generation resets.
        return bool(self.verified_clients)

    def request(self, lane, client=None):
        try:
            snapshot = self.fresh()
            if not self.matches(snapshot) or not snapshot['business_ready']:
                return dict(error=unavailable(snapshot))
            if self.config['version'] == 4 and client not in self.verified_clients:
                return dict(error='service_binding_unavailable')
            value = snapshot['binding']
            return dict(port=value['operations_port' if lane == 'control' else 'port'],
                        token=value['gateway_token'])
        except (OSError, ValueError, RuntimeError, KeyError):
            return dict(error=unavailable())


class AdmissionServer:
    """At most eight bounded internal GETs; no body, queue, DNS or worker thread."""
    def __init__(self, secret, clients, decide):
        self.secret, self.clients, self.decide = secret, frozenset(clients), decide
        self.selector = selectors.DefaultSelector()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(8)
        self.listener.setblocking(False)
        self.selector.register(self.listener, selectors.EVENT_READ)
        self.connections = {}

    @property
    def port(self):
        return self.listener.getsockname()[1]

    def drop(self, sock):
        self.connections.pop(sock, None)
        self.selector.unregister(sock)
        sock.close()

    def close(self):
        for sock in list(self.connections):
            self.drop(sock)
        self.selector.close()
        self.listener.close()

    def response(self, raw):
        try:
            head, sep, body = raw.partition(b'\r\n\r\n')
            ledger.require(sep and not body and len(head) <= 4096,
                           'admission header/body invalid')
            lines = head.decode('ascii').split('\r\n')
            ledger.require(lines[0] in ('GET /authorize HTTP/1.0',
                                       'GET /authorize HTTP/1.1'),
                           'invalid admission method/path')
            headers = {}
            for line in lines[1:]:
                name, sep, value = line.partition(':')
                name, value = name.lower(), value.strip()
                ledger.require(sep and name not in headers and
                               name in ('host', 'connection', 'content-length',
                                   'x-q4t-admission-secret', 'x-q4t-client',
                                   'x-q4t-lane') and '\x00' not in value,
                               'invalid admission header')
                headers[name] = value
            ledger.require(headers.get('content-length', '0') == '0' and
                hmac.compare_digest(headers.get('x-q4t-admission-secret', ''),
                                    self.secret) and
                headers.get('x-q4t-client') in self.clients and
                headers.get('x-q4t-lane') in ('control', 'data'),
                'unauthorized internal admission')
        except (ValueError, UnicodeError):
            return b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
        try:
            result = self.decide(headers['x-q4t-lane'])
            if result.keys() == {'port', 'token'}:
                ledger.require(type(result['port']) is int and
                    1024 <= result['port'] <= 65535 and
                    type(result['token']) is str and ledger.HASH.fullmatch(result['token']),
                    'invalid backend admission')
                fields = f"X-Q4T-Port: {result['port']}\r\nX-Q4T-Token: {result['token']}\r\n"
                status = '204 No Content'
            else:
                code = result.get('error', '')
                ledger.require(code in gateway_config.ERRORS, 'invalid admission error')
                fields, status = f'X-Q4T-Error: {code}\r\n', '503 Service Unavailable'
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            fields, status = ('X-Q4T-Error: service_binding_unavailable\r\n',
                              '503 Service Unavailable')
        return (f'HTTP/1.1 {status}\r\nContent-Length: 0\r\n'
                f'Connection: close\r\n{fields}\r\n').encode('ascii')

    def step(self, timeout=0.05):
        for key, mask in self.selector.select(timeout):
            sock = key.fileobj
            if sock is self.listener:
                client, _ = self.listener.accept()
                if len(self.connections) >= 8:
                    client.close()
                    continue
                client.setblocking(False)
                self.connections[client] = [time.monotonic()+2, bytearray(), None]
                self.selector.register(client, selectors.EVENT_READ)
                continue
            item = self.connections[sock]
            try:
                if mask & selectors.EVENT_READ:
                    part = sock.recv(4097-len(item[1]))
                    if not part:
                        self.drop(sock)
                        continue
                    item[1].extend(part)
                    if len(item[1]) > 4096:
                        self.drop(sock)
                        continue
                    if b'\r\n\r\n' in item[1]:
                        item[2] = self.response(bytes(item[1]))
                        self.selector.modify(sock, selectors.EVENT_WRITE)
                elif mask & selectors.EVENT_WRITE:
                    count = sock.send(item[2])
                    item[2] = item[2][count:]
                    if not item[2]:
                        self.drop(sock)
            except BlockingIOError:
                continue
            except OSError:
                self.drop(sock)
        now = time.monotonic()
        for sock, item in list(self.connections.items()):
            if now >= item[0]:
                self.drop(sock)


def run(config, channel):
    gateway_config.validate(config)
    directory = Path(config['directory'])
    info = directory.lstat()
    ledger.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and
                   stat.S_IMODE(info.st_mode) == 0o700,
                   'private gateway runtime parent required')
    ledger.require(legacy.sha(config['nginx']) == config['nginx_sha256'],
                   'nginx binary identity changed')
    runtime = directory/f'gateway-{channel.generation}'
    runtime.mkdir(mode=0o700)
    for name in ('body', 'proxy', 'fastcgi', 'uwsgi', 'scgi'):
        (runtime/name).mkdir(mode=0o700)
    stopped = False
    heartbeat = 0
    process = None
    def stop(_signal, _frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def tick():
        nonlocal heartbeat
        if stopped or (process is not None and process.poll() is not None):
            return True
        if time.monotonic() >= heartbeat:
            channel.event('heartbeat')
            heartbeat = time.monotonic()+0.25
        return False
    admission = Admission(config, channel, stopped=tick)
    secret = secrets.token_hex(32)
    server = AdmissionServer(secret, (c['id'] for c in config['policy']['clients']),
                             admission.request)
    try:
        text = gateway_config.render(config, runtime, server.port, secret)
        legacy.save(runtime/'nginx.conf', text.encode())
        process = subprocess.Popen([sys.executable, '-I', '-B',
            str(ROOT/'tools/serve_recovery/cpu_exec.py'), '--parent-pid', str(os.getpid()),
            '--parent-start', str(journal.process_start(os.getpid())), '--',
            config['nginx'], '-p', str(runtime)+'/', '-c', str(runtime/'nginx.conf'),
            '-e', '/dev/null'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        end = time.monotonic()+10
        while not tick():
            try:
                owner = listener(process.pid, config['policy']['listen_host'],
                                 config['policy']['listen_port'], config['nginx_sha256'])
                ledger.require(owner['executable_sha256'] == config['nginx_sha256'],
                               'nginx listener identity changed')
                break
            except (OSError, ValueError, RuntimeError):
                ledger.require(time.monotonic() < end, 'nginx listener startup deadline')
                server.step()
        ledger.require(not tick(), 'nginx exited during startup')
        channel.event('ready')
        next_update = 0
        while not tick():
            if time.monotonic() >= next_update:
                admission.update()
                next_update = time.monotonic()+0.25
            server.step()
    finally:
        server.close()
        admission.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    # Guardian retains ownership, capacity and lease until reap.
                    pass
        channel.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = legacy.parse(legacy.read_file(args.config, private=True))
    run(config, control.GatewayChannel())


if __name__ == '__main__':
    main()
