"""Explicit independent outer process; startup replies never own core health."""
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from tools.serve_host import native
from tools.serve_recovery import bundle, deployment, journal, ledger, lifecycle
from tools.serve_recovery import outer_manager, outer_runtime, resource_launch
from tools.serve_service import package as engine


def inherited_environment():
    """Duplicate only declared capabilities; never consume a cold token."""
    env = dict(os.environ); copies = []; seen = set(); groups = []
    try:
        for name, count in (('Q4T_MANAGED_LEASE_FD', 1), (resource_launch.ENTRY_FD, 1),
                            ('Q4T_DEVICE_ADMISSION_FDS', 4)):
            if name not in env: continue
            fields = env[name].split(',')
            ledger.require(len(fields) == count and all(x.isdecimal() and len(x) <= 7 for x in fields),
                           'invalid outer daemon inherited capabilities')
            numbers = list(map(int, fields))
            for number in numbers:
                ledger.require(3 <= number <= 1000000 and number not in seen,
                               'outer daemon capabilities collide')
                seen.add(number); os.fstat(number)
            groups.append((name, numbers))
        # Validate every original FD before allocating any duplicate: a bad
        # source number must not accidentally become a newly allocated copy.
        for name, numbers in groups:
            forwarded = []
            for number in numbers:
                fd = os.dup(number); copies.append(fd); forwarded.append(fd)
                os.set_inheritable(fd, False)
            env[name] = ','.join(map(str, forwarded))
        return env, tuple(copies)
    except BaseException:
        for fd in copies: os.close(fd)
        raise


def start(state, package, digest, *, activate=True, timeout=360):
    ledger.require(type(activate) is bool and type(timeout) is int and 1 <= timeout <= 600,
                   'bounded explicit outer daemon startup required')
    state, package = Path(state).absolute(), Path(package).resolve(strict=True)
    manifest = bundle.verify(package, digest)
    ledger.require(manifest['version'] >= 18, 'outer daemon requires service profile v18')
    with deployment.claim(state, shared=True):
        desc = lifecycle.selected_state(state, package, digest); outer_manager.ready(state, desc)
        parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        copies = (); diagnostics = None
        try:
            env, copies = inherited_environment()
            directory = state/'outer-runs'; directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            ledger.require(directory.resolve(strict=True) == directory and info.st_uid == os.geteuid()
                and info.st_mode & 0o777 == 0o700, 'private outer startup directory required')
            run = directory/str(uuid.uuid4()); run.mkdir(mode=0o700)
            config = dict(version=1, state=str(state), package=str(package), manifest_sha256=digest,
                service_id=desc['service_id'], activate=activate, startup_fd=endpoint.fileno(),
                parent_pid=os.getpid())
            engine.save(run/'outer.json', config)
            from tools.serve_recovery import service
            try: diagnostics = service.diagnostic_buffer()
            except OSError: pass
            process = subprocess.Popen([manifest['python']['path'], '-I', '-B',
                str(package/'source/tools/serve_recovery/outer_daemon.py'), '--config', str(run/'outer.json')],
                stdin=subprocess.DEVNULL, stdout=diagnostics if diagnostics is not None else subprocess.DEVNULL,
                stderr=diagnostics if diagnostics is not None else subprocess.DEVNULL,
                start_new_session=True, close_fds=True, pass_fds=(*copies, endpoint.fileno()), env=env)
            endpoint.close(); endpoint = None
            deadline = time.monotonic()+timeout
            while time.monotonic() < deadline:
                ready, _, _ = select.select([parent], [], [], min(.1, max(0, deadline-time.monotonic())))
                if ready:
                    value = journal.receive(parent)
                    if value is None: break
                    ledger.require(type(value) is dict and value.get('version') == 1 and
                        value.get('service_id') == desc['service_id'] and value.get('outer_pid') == process.pid
                        and value.get('manifest_sha256') == digest, 'outer startup reply identity changed')
                    confirmed = value.get('business_ready') is True or (not activate and
                        value.get('state') == 'STOPPED' and value.get('cleanup_confirmed') is True)
                    return dict(value, daemon_start=True, startup_confirmed=confirmed, run=str(run))
                if process.poll() is not None: break
            # --daemon authorizes an independent service. A lost/late reply
            # cannot rewrite its intent or send a model/Guardian signal.
            return dict(status='UNKNOWN', state='STARTING' if process.poll() is None else 'EXITED',
                business_ready=False, daemon_start=True, startup_receipt='UNKNOWN',
                service_id=desc['service_id'], outer_pid=process.pid, outer_exit_code=process.returncode,
                run=str(run))
        finally:
            parent.close()
            if endpoint is not None: endpoint.close()
            for fd in copies: os.close(fd)
            if diagnostics is not None: os.close(diagnostics)


class Reply:
    def __init__(self, value):
        self.value = value; self.socket = socket.socket(fileno=value['startup_fd'])
        ledger.require(self.socket.family == socket.AF_UNIX and
            self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_SEQPACKET,
            'private outer startup channel required')
        peer = struct.unpack('3i', self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        ledger.require(peer[:2] == (value['parent_pid'], os.geteuid()), 'outer startup peer changed')
        self.socket.setblocking(False)

    def send(self, status, *, final=False):
        if self.socket is None or not final and status.get('business_ready') is not True: return
        try:
            journal.send(self.socket, dict(status, version=1, service_id=self.value['service_id'],
                outer_pid=os.getpid(), manifest_sha256=self.value['manifest_sha256']))
        except (OSError, ValueError, KeyError, TypeError): pass
        finally: self.close()

    def close(self):
        if self.socket is not None: self.socket.close(); self.socket = None


def run(path):
    value = native.private_json(path, 16384)
    ledger.keys(value, ('version', 'state', 'package', 'manifest_sha256', 'service_id',
                       'activate', 'startup_fd', 'parent_pid'))
    ledger.require(type(value['version']) is int and value['version'] == 1 and
        type(value['activate']) is bool and ledger.integer(value['startup_fd'], minimum=3) and
        ledger.integer(value['parent_pid'], minimum=1) and ledger.identifier(value['service_id']),
        'invalid outer daemon configuration')
    reply = Reply(value)
    try:
        desc = lifecycle.selected_state(Path(value['state']), value['package'], value['manifest_sha256'])
        ledger.require(desc['service_id'] == value['service_id'], 'outer daemon deployment changed')
        result = outer_runtime.run(value['state'], value['package'], value['manifest_sha256'],
            activate=value['activate'], startup=reply.send)
        reply.send(result, final=True)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        reply.send(dict(status='FAILED', business_ready=False, category=type(error).__name__), final=True)
        return 1
    finally: reply.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    return run(parser.parse_args().config)


if __name__ == '__main__': sys.exit(main())
