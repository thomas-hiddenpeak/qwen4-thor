"""Private limits proposals, idempotent commits and verified live application."""
import fcntl
import os
from pathlib import Path
import time
import uuid

from tools.serve_gateway import client_policy as cp
from tools.serve_recovery import access_manager, access_store, ledger, model_admin
from tools.serve_recovery import quota_control, quota_policy as policy, quota_store, store
from tools.serve_service import package as engine

PROPOSAL = 'q4t-client-limits-proposal-v1'


def failure(error):
    code = ('limits_writer_busy' if isinstance(error, BlockingIOError) else
        'limits_io_unavailable' if isinstance(error, OSError) else
        {'access revision conflict': 'limits_revision_conflict',
         'access idempotency conflict': 'limits_idempotency_conflict',
         'limits proposal identity changed': 'limits_proposal_identity_mismatch',
         'client fairness exceeds global capacity': 'limits_capacity_exceeded'}
        .get(str(error), 'limits_policy_rejected'))
    return dict(status='FAILED', category=type(error).__name__, code=code,
        commit_state='UNKNOWN', live_applied=False, model_actions=False)


def base(desc):
    _, value = cp.read(Path(desc['package'])/'engine/client-policy.json')
    return policy.from_base(value)


def capacity(value, desc):
    # The managed profile passes explicit global admission limits to the
    # model; budget-driven sequence capacity does not rewrite these values.
    profile = engine.read(Path(desc['package'])/'engine/manifest.json')['settings']['profile']
    return policy.capacity(value, profile['max_inflight_requests'], profile['max_queued_requests'])


def initialize(state, desc, manifest):
    ledger.require(manifest.get('ingress_mode') == 'native', 'native limits profile required')
    initial = capacity(base(desc), desc)
    return quota_store.initialize_bound(access_store.binding(state, desc['service_id']), initial)


def migrate(state, desc, manifest):
    ledger.require(manifest['version'] < 8, 'new profiles must retain initialized limits state')
    state = Path(state)
    fd = os.open(state/'guardian.lock', os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        held = store.check_file(fd);fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        named = (state/'guardian.lock').stat(follow_symlinks=False)
        ledger.require((held.st_dev, held.st_ino) == (named.st_dev, named.st_ino),
                       'guardian ownership lock changed')
        with store.Store(state, desc['service_id']) as core:
            ledger.require(core.state['desired'] == 'STOPPED' and not any(core.state['active'].values()),
                           'limits migration requires durable STOPPED')
            return initialize(state, desc, manifest)
    finally:os.close(fd)


def open_journal(state, service_id):
    return quota_store.open_bound(access_store.binding(state, service_id))


def propose(state, desc, limits, destination, request_id=None):
    request_id = str(uuid.uuid4()) if request_id is None else request_id
    ledger.require(ledger.identifier(request_id), 'canonical limits request UUID required')
    value = access_manager.private_json(limits)
    ledger.keys(value, ('version', 'clients'))
    ledger.require(type(value['version']) is int and value['version'] == 1, 'limits input version')
    with open_journal(state, desc['service_id']) as current:
        previous = current.head['state']['policy']
        candidate = capacity(policy.desired(previous, value['clients']), desc)
        proposal = dict(format=PROPOSAL, service_id=desc['service_id'],
            state_identity=current.state_identity, request_id=request_id,
            expected_revision=previous['revision'], policy=candidate)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:store.Store.write_all(fd, ledger.canonical(proposal));os.fsync(fd)
    finally:os.close(fd)
    directory = os.open(Path(destination).absolute().parent, os.O_RDONLY | os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)
    return dict(proposed=True, request_id=request_id, expected_revision=previous['revision'],
                candidate=policy.describe(candidate), committed=False, live_applied=False)


def live(desc, manifest):
    snapshot = model_admin.observe(desc, manifest)
    generation = snapshot['model_generation']
    owner = dict(pid=snapshot['model_pid'], start_ticks=snapshot['model_start_ticks'])
    # The native socket is tied to the exact live Guardian model owner. A
    # same-UID process at a lookalike address cannot satisfy PID/birth checks.
    channel = quota_control.Channel(quota_control.address(desc['service_id'], generation))
    try:
        deadline = time.monotonic()+1
        while time.monotonic()<deadline:
            result = channel.step(owner['pid'], owner['start_ticks'])
            if result['observed'] is not None and not result['error']:
                model_admin.recheck(desc, snapshot)
                return dict(result['observed'], model_pid=owner['pid'],
                            model_start_ticks=owner['start_ticks'], model_generation=generation)
            time.sleep(.01)
        raise TimeoutError('model limits observation deadline')
    finally:channel.close()


def status(state, desc, manifest, request_id=None):
    if request_id is not None:ledger.require(ledger.identifier(request_id), 'invalid limits request UUID')
    result = dict(durable=None, durable_error='', live=None, live_error='', receipt=None,
                  receipt_known=False, live_applied=False, model_actions=False)
    try:
        with open_journal(state, desc['service_id']) as current:
            result['durable'] = policy.describe(current.head['state']['policy'])
            if request_id is not None:
                result['receipt'] = next((r for r in current.head['state']['receipts']
                                          if r['id'] == request_id), None)
                result['receipt_known'] = result['receipt'] is not None
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        result['durable_error'] = failure(error)['code']
    try:result['live'] = live(desc, manifest)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):result['live_error'] = 'limits_live_unknown'
    observed, durable = result['live'], result['durable']
    result['live_applied'] = bool(observed and durable and observed['configured'] and
        observed['revision'] == durable['revision'] and observed['content_sha256'] == durable['sha256'])
    return result


def apply(state, desc, manifest, proposal_path, *, wait_seconds=3):
    ledger.require(ledger.integer(wait_seconds) and wait_seconds <= 10, 'limits wait bound')
    proposal = access_manager.private_json(proposal_path)
    ledger.keys(proposal, ('format', 'service_id', 'state_identity', 'request_id', 'expected_revision', 'policy'))
    identity = access_store.binding(state, desc['service_id'])
    ledger.require(proposal['format'] == PROPOSAL and proposal['service_id'] == desc['service_id'] and
        proposal['state_identity'] == identity['state_identity'], 'limits proposal identity changed')
    capacity(policy.validate(proposal['policy']), desc)
    with open_journal(state, desc['service_id']) as current:
        receipt = current.transact(proposal['request_id'], proposal['expected_revision'], proposal['policy'])
    deadline = time.monotonic()+wait_seconds
    while True:
        observed, error = None, ''
        try:observed = live(desc, manifest)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):error = 'limits_live_unknown'
        applied = bool(observed and observed['configured'] and observed['revision'] == receipt['revision'] and
                       observed['content_sha256'] == receipt['policy_sha256'])
        if applied or time.monotonic()>=deadline:break
        time.sleep(.05)
    return dict(receipt, live=observed, live_error=error, live_applied=applied, model_actions=False)
