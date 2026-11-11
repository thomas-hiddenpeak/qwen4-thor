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
        if hasattr(channel, 'fixture_tick'):
            channel.fixture_tick()
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
    probe_proof = dict(cleanup_complete=True, resources_safe=True,
        identity_valid=True, lease_continuous=True, device_usable=False)
    if args.probe_only or args.probe_lifecycle and channel.generation > 1:
        reply = channel.transact('probe', dict(role='model', proof=probe_proof))
        (args.model_fixture/f'probe-controller-{channel.generation}.json').write_text(json.dumps(reply))
        idle(channel)
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
            value = dict(port=18000, operations_port=18001,
                    gateway_token=hashlib.sha256(grant['ticket'].encode()).hexdigest(),
                    client_policy_sha256='b'*64, binary_sha256=args.binary_fingerprint,
                    protocol_sha256=args.protocol_fingerprint)
            if args.capacity_fingerprint is not None:
                value['capacity_sha256'] = args.capacity_fingerprint
                reply = channel.transact('bind', dict(role='model', ticket=grant['ticket'], binding=value))
                (args.model_fixture/'capacity-binding.json').write_text(json.dumps(reply))
                if not reply['ok']: idle(channel)
            else:
                transaction(channel, 'bind', dict(role='model', ticket=grant['ticket'], binding=value))
        if args.stop_handoff:
            def planned_stop(_signal, _frame):
                # Synthetic controller work measures actual signal ordering;
                # it does not pretend to save a real model quota snapshot.
                before = process.poll() is None
                time.sleep(.15)
                after = process.poll() is None
                (args.model_fixture/'handoff-order.json').write_text(json.dumps(
                    dict(before_alive=before, after_alive=after, cpu_fixture=True)))
                cleanup_children()
                raise SystemExit(0)
            signal.signal(signal.SIGTERM, planned_stop)
        if args.quota_restore and grant['generation'] > 1:
            generation = grant['generation']
            event = dict(model_generation=generation, source_generation=generation-1,
                         boot_id=owner['boot_id'], receipt_sha256='a'*64)
            if args.quota_restore == 'stale':event['model_generation'] -= 1
            if args.quota_restore == 'boot':event['boot_id'] = '00000000-0000-0000-0000-000000000000'
            if args.quota_restore == 'digest':event['receipt_sha256'] = 'invalid'
            channel._send('quota_restored', quota_restored=event)
            if args.quota_restore == 'valid':quota_deadline = 0
        ready = dict(role='model', ticket=grant['ticket'], verified=True)
        if time.monotonic_ns() < quota_deadline:
            early = channel.transact('ready', ready)
            ledger.require(not early['ok'] and
                           early['result']['reason'] == 'quota_window',
                           'early quota admission incorrectly accepted')
            (args.model_fixture/'quota-blocked.json').write_text(json.dumps(early))
            idle(channel, (quota_deadline-time.monotonic_ns())/1e9)
        transaction(channel, 'ready', ready)
        if os.environ.get('Q4T_VERSION_RECOVERY') == '1':
            def version_hook():
                fatal=args.model_fixture/'cpu-fatal-close-first'
                if fatal.exists():
                    # This CPU case isolates EOF-before-wait, not the host's
                    # apport/core-dump pipeline. Disable dumps only in this
                    # about-to-exit fixture, after its identities were checked.
                    if libc.prctl(4,0,0,0,0):
                        raise RuntimeError('CPU fixture dump isolation failed')
                    trace=args.model_fixture/'cpu-fatal-close-timing.jsonl'
                    with trace.open('a') as output:
                        output.write(json.dumps(dict(stage='before_close',pid=os.getpid(),
                            dumpable=False,at_ns=time.monotonic_ns()))+'\n')
                    channel.sock.close()
                    time.sleep(.05)
                    with trace.open('a') as output:
                        output.write(json.dumps(dict(stage='before_signal',pid=os.getpid(),
                                                     at_ns=time.monotonic_ns()))+'\n')
                    os.kill(os.getpid(),signal.SIGABRT)
                marker=args.model_fixture/'model-exit-report.json'
                if marker.exists():
                    channel._send('model_exit',model_exit=json.loads(marker.read_text()))
                    marker.unlink()
            channel.fixture_tick=version_hook
        if args.aux_health:
            def hook():
                invalid_auxiliary = args.model_fixture/'auxiliary-invalid.json'
                if invalid_auxiliary.exists():
                    channel.health('READY', 'AVAILABLE', 'serving',
                        auxiliary_io=json.loads(invalid_auxiliary.read_text()))
                else:
                    channel.health('READY', 'UNKNOWN', 'query_timeout')
                channel.admission(grant['generation'], not (args.model_fixture/'admission.closed').exists())
            channel.fixture_tick = hook
        while process.poll() is None:
            idle(channel, 0.05)
        if os.environ.get('Q4T_VERSION_RECOVERY') == '1' and process.returncode in (-4,-6,-11):
            channel.model_exit(grant['generation'],owner,process.returncode)
        fault = dict(generation=grant['generation'],
            fingerprint=hashlib.sha256(b'cpu-fixture-signal-exit').hexdigest(),
            unknown=True)
        transaction(channel, 'fault', fault)
        cleanup_children()
        transaction(channel, 'retired', dict(role='model', ticket=grant['ticket'],
                                            verified=True))
        if args.probe_lifecycle:
            replies = [channel.transact('probe', dict(role='model', proof=probe_proof)) for _ in range(2)]
            (args.model_fixture/'probes.json').write_text(json.dumps(replies))
            idle(channel)


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
