"""CPU model/controller actors for native Guardian integration tests only."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import journal, ledger


def idle(channel, seconds=None):
    end = time.monotonic()+seconds if seconds is not None else float('inf')
    while time.monotonic() < end:
        channel.event('heartbeat')
        time.sleep(0.05)


def transaction(channel, operation, args):
    reply = channel.transact(operation, args)
    if not reply['ok']:
        # Journal failure must not turn into a controller/model crash loop.
        idle(channel)
    return reply['result']


def cleanup_children():
    end = time.monotonic()+1
    while time.monotonic() < end:
        children = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry/'stat').read_text().rsplit(')', 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if int(fields[1]) == os.getpid():
                children.append(int(entry.name))
        if not children:
            return
        for pid in children:
            try:
                fd = os.pidfd_open(pid)
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                    os.waitpid(pid, os.WNOHANG)
                finally:
                    os.close(fd)
            except (ProcessLookupError, ChildProcessError):
                pass
        time.sleep(0.01)
    raise RuntimeError('fixture child cleanup unconfirmed')


def controller(channel, args):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):
        raise RuntimeError('fixture subreaper unavailable')
    lease_fd = int(os.environ['Q4T_LEASE_FD'])
    while True:
        proof = dict(cleanup_complete=True, identity_valid=True,
            resources_safe=True, lease_continuous=True,
            device_usable=not args.device_unknown)
        grant = transaction(channel, 'authorize', dict(role='model', proof=proof))
        if not grant['allowed']:
            if 'retry_after_ns' in grant:
                idle(channel, grant['retry_after_ns']/1e9)
                continue
            idle(channel)
        quota_deadline = grant.get('quota_not_before_monotonic_ns', 0)
        grant = grant['grant']
        if args.pause_at == 'authorized':
            idle(channel)
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        marker = args.model_fixture/f"model-{grant['generation']}.started"
        command = [sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                   '--marker', str(marker)]
        if args.escape_model_child:
            command.append('--escape-child')
        process = subprocess.Popen([sys.executable, '-I', '-B',
            str(ROOT/'tools/serve_recovery/child.py'), '--fd', str(child.fileno()),
            '--parent-pid', str(os.getpid()), '--parent-start',
            str(journal.process_start(os.getpid())), '--lease-fd', str(lease_fd),
            '--lease-path', os.environ['Q4T_LEASE_PATH'],
            '--controller-generation', str(channel.generation), '--', *command],
            pass_fds=(child.fileno(), lease_fd), start_new_session=True)
        child.close()
        try:
            parent.settimeout(2)
            owner = journal.receive(parent)
            ledger.require(owner.pop('version') == 1, 'invalid CPU child hello')
            transaction(channel, 'spawned', dict(role='model',
                        ticket=grant['ticket'], owner=owner))
            if args.pause_at == 'spawned':
                idle(channel)
            journal.send(parent, dict(version=1, ticket=grant['ticket'],
                controller_generation=channel.generation,
                model_generation=grant['generation']))
        finally:
            parent.close()
        end = time.monotonic()+3
        while not marker.exists():
            if process.poll() is not None or time.monotonic() >= end:
                raise RuntimeError('CPU model did not pass exec barrier')
            idle(channel, 0.05)
        if args.bind_model:
            transaction(channel, 'bind', dict(role='model', ticket=grant['ticket'],
                binding=dict(port=18000, operations_port=18001,
                    gateway_token=hashlib.sha256(grant['ticket'].encode()).hexdigest(),
                    client_policy_sha256='b'*64, binary_sha256='c'*64,
                    protocol_sha256='d'*64)))
        ready = dict(role='model', ticket=grant['ticket'], verified=True)
        if time.monotonic_ns() < quota_deadline:
            early = channel.transact('ready', ready)
            ledger.require(not early['ok'] and
                           early['result']['reason'] == 'quota_window',
                           'early quota admission incorrectly accepted')
            (args.model_fixture/'quota-blocked.json').write_text(json.dumps(early))
            idle(channel, (quota_deadline-time.monotonic_ns())/1e9)
        transaction(channel, 'ready', ready)
        while process.poll() is None:
            idle(channel, 0.05)
        fault = dict(generation=grant['generation'],
            fingerprint=hashlib.sha256(b'cpu-fixture-signal-exit').hexdigest(),
            unknown=True)
        transaction(channel, 'fault', fault)
        cleanup_children()
        transaction(channel, 'retired', dict(role='model', ticket=grant['ticket'],
                                            verified=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--marker', type=Path, required=True)
    parser.add_argument('--escape-child', action='store_true')
    args = parser.parse_args()
    if args.escape_child:
        child = os.fork()
        if child == 0:
            os.setsid()
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                signal.pause()
    args.marker.write_text(json.dumps(dict(pid=os.getpid(),
        start_ticks=journal.process_start(os.getpid()), cpu_fixture=True))+'\n')
    while True:
        signal.pause()


if __name__ == '__main__':
    main()
