"""Explicit private credential administration; no model or recovery actions."""
import fcntl
import os
from pathlib import Path
import select
import socket
import stat
import struct
import time
import uuid

from tools.serve_gateway import gateway as files
from tools.serve_recovery import access_control, access_policy as policy, access_store
from tools.serve_recovery import control, journal, ledger, store

PROPOSAL = 'q4t-client-access-proposal-v1'


def failure(error):
    codes = {
        'access revision conflict': 'credential_revision_conflict',
        'access idempotency conflict': 'credential_idempotency_conflict',
        'credential proposal identity changed': 'credential_proposal_identity_mismatch',
        'historical key ownership changed': 'credential_owner_conflict',
        'client membership requires coordinated backend policy publication': 'credential_backend_membership_pending',
    }
    code = ('credential_writer_busy' if isinstance(error, BlockingIOError) else
            'credential_io_unavailable' if isinstance(error, OSError) else
            codes.get(str(error), 'credential_policy_rejected'))
    return dict(status='FAILED', category=type(error).__name__, code=code,
        commit_state='UNKNOWN', live_applied=False, model_actions=False)


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        ledger.require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid() and
            before.st_nlink == 1 and not stat.S_IMODE(before.st_mode) & 0o077 and
            0 < before.st_size <= policy.MAX_BYTES, 'private bounded credential input required')
        raw = bytearray()
        while len(raw) <= before.st_size:
            part = os.read(fd, min(65536, before.st_size + 1 - len(raw)))
            if not part:break
            raw.extend(part)
        after = os.fstat(fd)
        ledger.require(len(raw) == before.st_size and all(getattr(before, k) == getattr(after, k)
            for k in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')),
            'credential input changed')
        return store.decode(raw)
    finally:
        os.close(fd)


def initialize(state, desc, manifest):
    ledger.require(manifest.get('ingress_mode') == 'native', 'native credential profile required')
    value = files.policy(private_json(Path(desc['package']) / 'gateway-policy.json'))
    initial = policy.from_keys([dict(id=c['id'], api_keys=[c['api_key']]) for c in value['clients']])
    return access_store.initialize_bound(access_store.binding(state, desc['service_id']), initial)


def migrate(state, desc, manifest):
    """Old STOPPED profiles only. Permanent marker forbids reinitialization."""
    ledger.require(manifest['version'] < 7, 'new profiles must retain initialized credential state')
    state = Path(state)
    fd = os.open(state / 'guardian.lock', os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        held = store.check_file(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        named = (state / 'guardian.lock').stat(follow_symlinks=False)
        ledger.require((held.st_dev, held.st_ino) == (named.st_dev, named.st_ino),
                       'guardian ownership lock changed')
        with store.Store(state, desc['service_id']) as core:
            ledger.require(core.state['desired'] == 'STOPPED' and not any(core.state['active'].values()),
                           'credential migration requires durable STOPPED')
            return initialize(state, desc, manifest)
    finally:
        os.close(fd)


def open_journal(state, service_id):
    value = access_store.binding(state, service_id)
    return access_store.open_bound(value)


def allowed_clients(value, desc, state, previous):
    # Rotation/revocation of existing identities must remain available if the
    # unrelated limits journal fails. Only adding an identity needs its proof.
    if {c['id'] for c in value['clients']} <= {c['id'] for c in previous['clients']}:return
    manifest = private_json(Path(desc['package']) / 'manifest.json')
    if manifest['version'] >= 8 and manifest.get('ingress_mode') == 'native':
        from tools.serve_recovery import quota_store
        with quota_store.open_bound(access_store.binding(state, desc['service_id'])) as limits:
            known = {c['id'] for c in limits.head['state']['policy']['clients']}
        ledger.require({c['id'] for c in value['clients']} <= known,
                       'client membership requires coordinated backend policy publication')
        return
    base = private_json(Path(desc['package']) / 'engine/client-policy.json')
    ledger.require({c['id'] for c in value['clients']} == {c['id'] for c in base['clients']},
                   'client membership requires coordinated backend policy publication')


def propose(state, desc, keys, destination, request_id=None):
    request_id = str(uuid.uuid4()) if request_id is None else request_id
    ledger.require(ledger.identifier(request_id), 'canonical credential request UUID required')
    value = private_json(keys)
    ledger.keys(value, ('version', 'clients'))
    ledger.require(type(value['version']) is int and value['version'] == 1, 'credential input version')
    with open_journal(state, desc['service_id']) as current:
        previous = current.head['state']['policy']
        candidate = policy.from_keys(value['clients'], previous)
        allowed_clients(candidate, desc, state, previous)
        proposal = dict(format=PROPOSAL, service_id=desc['service_id'],
            state_identity=current.state_identity, request_id=request_id,
            expected_revision=previous['revision'], policy=candidate)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        store.Store.write_all(fd, ledger.canonical(proposal));os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(Path(destination).absolute().parent, os.O_RDONLY | os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)
    return dict(proposed=True, request_id=request_id, expected_revision=previous['revision'],
        candidate=policy.describe(candidate), committed=False, live_applied=False)


def live(desc, manifest):
    snapshot = control.request(desc['service_id'], 'status', timeout=1)
    parent, start, generation = (snapshot.get(n) for n in
        ('gateway_pid', 'gateway_start_ticks', 'gateway_generation'))
    ledger.require(ledger.integer(parent, minimum=2) and ledger.integer(start, minimum=1) and
        ledger.integer(generation, minimum=1), 'native credential entrypoint unavailable')
    address = f'q4t-ingress-{os.geteuid()}-{desc["service_id"]}-{generation}'
    observer = access_control.Publisher(address)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as channel:
        channel.settimeout(1)
        channel.connect('\0' + address + '-access')
        pid, uid, _ = struct.unpack('3i', channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        ledger.require(uid == os.geteuid() and journal.process_start(parent) == start,
                       'credential parent identity changed')
        birth = journal.process_start(pid)
        fd = os.pidfd_open(pid)
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            ledger.require(int(fields[1]) == parent and
                files.sha(f'/proc/{pid}/exe') == manifest['files']['bin/q4t-ingress'] and
                journal.process_start(pid) == birth, 'credential entrypoint identity changed')
            channel.send(b'{"op":"status"}')
            observer.socket = channel
            ok, status = observer.reply()
            ledger.require(ok and not select.select([fd], [], [], 0)[0] and
                journal.process_start(parent) == start, 'credential observation changed')
            return dict(status, entrypoint_pid=pid, entrypoint_start_ticks=birth,
                        gateway_generation=generation)
        finally:
            observer.socket = None
            os.close(fd)


def status(state, desc, manifest, request_id=None):
    ledger.require(request_id is None or ledger.identifier(request_id), 'invalid credential request UUID')
    result = dict(service_id=desc['service_id'], durable=None, live=None, live_applied=False,
                  durable_error='', live_error='', request_id=request_id, receipt=None)
    try:
        with open_journal(state, desc['service_id']) as current:
            result['durable'] = policy.describe(current.head['state']['policy'])
            if request_id is not None:
                result['receipt'] = next((r for r in current.head['state']['receipts']
                                          if r['id'] == request_id), None)
    except (OSError, ValueError, RuntimeError):
        result['durable_error'] = 'credential_state_unknown'
    try:
        result['live'] = live(desc, manifest)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        result['live_error'] = 'credential_live_unknown'
    result['live_applied'] = bool(result['durable'] and result['live'] and
        result['live']['configured'] and result['durable']['revision'] == result['live']['revision'] and
        result['durable']['sha256'] == result['live']['sha256'])
    return result


def apply(state, desc, manifest, proposal_path, *, wait_seconds=3):
    ledger.require(ledger.integer(wait_seconds) and wait_seconds <= 10, 'credential wait bound')
    proposal = private_json(proposal_path)
    ledger.keys(proposal, ('format', 'service_id', 'state_identity', 'request_id',
                           'expected_revision', 'policy'))
    binding = access_store.binding(state, desc['service_id'])
    ledger.require(proposal['format'] == PROPOSAL and proposal['service_id'] == desc['service_id'] and
        proposal['state_identity'] == binding['state_identity'], 'credential proposal identity changed')
    policy.validate(proposal['policy'])
    with open_journal(state, desc['service_id']) as current:
        allowed_clients(proposal['policy'], desc, state, current.head['state']['policy'])
        receipt = current.transact(proposal['request_id'], proposal['expected_revision'], proposal['policy'])
    end = time.monotonic() + wait_seconds
    observed, error = None, 'credential_live_unknown'
    while True:
        try:
            observed = live(desc, manifest);error = ''
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            observed = None;error = 'credential_live_unknown'
        applied = bool(observed and observed['configured'] and observed['revision'] == receipt['revision'] and
                       observed['sha256'] == receipt['policy_sha256'])
        if applied or time.monotonic() >= end:break
        time.sleep(.05)
    return dict(receipt, live=observed, live_error=error, live_applied=applied,
                model_actions=False)
