"""Bounded, versioned recovery transactions, with no I/O or hardware imports.

Only the private journal worker executes these transitions. Guardian supplies
verified process/lease evidence; a grant is usable only after durable commit.
Clock accounting is deliberately conservative across boots: downtime earns no
new budget. Wall-clock changes cannot expire a restart or a quarantine.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
import uuid

from tools.serve_recovery.policy import Desired

VERSION = 1
POLICY = 'q4t-recovery-v1'
ROLES = ('controller', 'model', 'gateway')
SECOND = 1_000_000_000
MINUTE = 60*SECOND
DAY = 24*60*MINUTE
MAX_INTEGER = (1 << 63)-1
MAX_BYTES = 64*1024
MAX_FAILURES = 16
HASH = re.compile(r'[0-9a-f]{64}\Z')
QUARANTINES = frozenset(('device_failure', 'identity_invalid',
    'cleanup_failed', 'repeated_model_fault', 'unknown_exit_repeated',
    'failure_history_full', 'reconciliation_required'))
PROOFS = frozenset(('cleanup_complete', 'resources_safe', 'identity_valid',
                   'lease_continuous', 'device_usable'))


class LedgerError(ValueError):
    pass


def require(value, message):
    if not value:
        raise LedgerError(message)


def integer(value, *, minimum=0):
    return type(value) is int and minimum <= value <= MAX_INTEGER


def identifier(value):
    if type(value) is not str:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def keys(value, expected):
    require(type(value) is dict and set(value) == set(expected),
            'unexpected record fields')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class Stamp:
    boot_id: str
    monotonic_ns: int

    def __post_init__(self):
        require(identifier(self.boot_id) and integer(self.monotonic_ns),
                'invalid boot/monotonic stamp')


def new_state(service_id, stamp):
    require(identifier(service_id), 'service_id must be a canonical UUID')
    return dict(version=VERSION, policy=POLICY, service_id=service_id,
        revision=0, desired=Desired.STOPPED.value, quarantine=None,
        clock=dict(boot_id=stamp.boot_id, monotonic_ns=stamp.monotonic_ns,
                   logical_ns=0),
        generations={role: 0 for role in ROLES},
        active={role: None for role in ROLES},
        attempts={role: [] for role in ROLES},
        not_before={role: 0 for role in ROLES},
        circuit_until={role: 0 for role in ROLES}, failures=[], last=None)


def owner_valid(owner):
    keys(owner, ('pid', 'start_ticks', 'boot_id'))
    require(integer(owner['pid'], minimum=1) and
            integer(owner['start_ticks'], minimum=1) and
            identifier(owner['boot_id']), 'invalid process identity')


def validate(state, service_id):
    keys(state, ('version', 'policy', 'service_id', 'revision', 'desired',
                 'quarantine', 'clock', 'generations', 'active', 'attempts',
                 'not_before', 'circuit_until', 'failures', 'last'))
    require(type(state['version']) is int and state['version'] == VERSION and
            state['policy'] == POLICY and state['service_id'] == service_id and
            identifier(service_id), 'ledger identity/version mismatch')
    require(integer(state['revision']) and
            state['desired'] in tuple(item.value for item in Desired),
            'invalid revision/desired state')
    require(state['quarantine'] is None or
            (type(state['quarantine']) is str and
             state['quarantine'] in QUARANTINES), 'invalid quarantine')
    clock = state['clock']
    keys(clock, ('boot_id', 'monotonic_ns', 'logical_ns'))
    Stamp(clock['boot_id'], clock['monotonic_ns'])
    require(integer(clock['logical_ns']), 'invalid logical clock')
    for name in ('generations', 'active', 'attempts', 'not_before',
                 'circuit_until'):
        keys(state[name], ROLES)
    for role in ROLES:
        generation = state['generations'][role]
        require(integer(generation), 'invalid generation')
        for field in ('not_before', 'circuit_until'):
            require(integer(state[field][role]), 'invalid deadline')
        entries = state['attempts'][role]
        require(type(entries) is list and len(entries) <= (6 if role ==
            'model' else 3), 'unbounded restart history')
        require(all(integer(at) and at <= clock['logical_ns'] for at in
                    entries) and sorted(entries) == entries,
                'invalid restart history')
        active = state['active'][role]
        if active is not None:
            keys(active, ('generation', 'ticket', 'phase', 'owner', 'ready_at',
                          'controller_generation'))
            require(integer(active['generation'], minimum=1) and
                    active['generation'] == generation and
                    identifier(active['ticket']) and active['phase'] in
                    ('authorized', 'spawned', 'ready'), 'invalid active grant')
            require(integer(active['controller_generation']) and
                    active['controller_generation'] <=
                    state['generations']['controller'],
                    'invalid controller binding')
            if active['phase'] == 'authorized':
                require(active['owner'] is None, 'unspawned grant has owner')
            else:
                owner_valid(active['owner'])
            require((active['phase'] == 'ready' and
                     integer(active['ready_at']) and
                     active['ready_at'] <= clock['logical_ns']) or
                    (active['phase'] != 'ready' and active['ready_at'] is None),
                    'invalid ready timestamp')
            if role in ('controller', 'model'):
                require(active['controller_generation'] ==
                        state['generations']['controller'],
                        'active grant belongs to old controller')
    failures = state['failures']
    require(type(failures) is list and len(failures) <= MAX_FAILURES,
            'unbounded failure history')
    for failure in failures:
        keys(failure, ('generation', 'fingerprint', 'unknown', 'at'))
        require(integer(failure['generation'], minimum=1) and
                failure['generation'] <= state['generations']['model'] and
                type(failure['fingerprint']) is str and
                HASH.fullmatch(failure['fingerprint']) is not None and
                type(failure['unknown']) is bool and
                integer(failure['at']) and failure['at'] <= clock['logical_ns'],
                'invalid failure')
    last = state['last']
    require((last is None) == (state['revision'] == 0),
            'missing transaction receipt')
    if last is not None:
        keys(last, ('id', 'digest', 'reply'))
        keys(last['reply'], ('revision', 'result'))
        require(identifier(last['id']) and type(last['digest']) is str and
                HASH.fullmatch(last['digest']) is not None and
                type(last['reply']['result']) is dict and
                integer(last['reply']['revision']) and
                last['reply']['revision'] == state['revision'],
                'invalid transaction receipt')
    require(len(canonical(state)) <= MAX_BYTES, 'ledger size limit')


def tick(state, stamp):
    clock = state['clock']
    elapsed = 0
    if clock['boot_id'] == stamp.boot_id:
        require(stamp.monotonic_ns >= clock['monotonic_ns'],
                'monotonic clock moved backwards')
        elapsed = stamp.monotonic_ns-clock['monotonic_ns']
    now = clock['logical_ns']+elapsed
    require(integer(now), 'logical clock overflow')
    clock.update(boot_id=stamp.boot_id, monotonic_ns=stamp.monotonic_ns,
                 logical_ns=now)
    for role in ROLES:
        window = DAY if role == 'model' else 15*MINUTE
        state['attempts'][role] = [at for at in state['attempts'][role]
                                   if now-at < window]
    return now


def denied(reason, retry_at=None):
    result = dict(allowed=False, reason=reason)
    if retry_at is not None:
        result['retry_at_logical_ns'] = retry_at
    return result


def active_grant(state, args):
    role = args['role']
    require(role in ROLES and identifier(args['ticket']), 'invalid grant key')
    active = state['active'][role]
    require(active is not None and active['ticket'] == args['ticket'],
            'stale or absent grant')
    return active


def authorize(state, args, now, request_id):
    keys(args, ('role', 'proof'))
    role = args['role']
    require(role in ROLES, 'invalid role')
    keys(args['proof'], PROOFS)
    require(all(type(value) is bool for value in args['proof'].values()),
            'proof flags must be boolean')
    if state['desired'] != Desired.RUNNING:
        return denied('operator_intent')
    if state['quarantine'] is not None:
        return denied('quarantined')
    if state['active'][role] is not None:
        return denied('unreconciled_grant')
    if role == 'controller' and state['active']['model'] is not None:
        return denied('old_model_not_retired')
    if role == 'model':
        controller = state['active']['controller']
        if controller is None or controller['phase'] == 'authorized':
            return denied('controller_not_spawned')
        if controller['owner']['boot_id'] != state['clock']['boot_id']:
            return denied('controller_not_current')
    required = ('cleanup_complete', 'identity_valid') if role == 'gateway' \
        else PROOFS
    if not all(args['proof'][field] for field in required):
        return denied('preflight_incomplete')
    wait_until = max(state['not_before'][role], state['circuit_until'][role])
    if now < wait_until:
        return denied('cooldown', wait_until)
    attempts = state['attempts'][role]
    exhausted_until = 0
    if role == 'model':
        recent = [at for at in attempts if now-at < 30*MINUTE]
        if len(recent) >= 3:
            exhausted_until = recent[-3]+30*MINUTE
        if len(attempts) >= 6:
            exhausted_until = max(exhausted_until, attempts[-6]+DAY)
    elif len(attempts) >= 3:
        exhausted_until = attempts[-3]+15*MINUTE
    if exhausted_until:
        until = max(exhausted_until, now+15*MINUTE)
        state['circuit_until'][role] = until
        return denied('budget_exhausted', until)
    # Never make a stopped/failed attempt free. Only generation zero gets its
    # initial start; explicit stop/start, release changes and controller death
    # all preserve counters and the monotonic generation number.
    if state['generations'][role] > 0:
        attempts.append(now)
    generation = state['generations'][role]+1
    require(integer(generation), 'generation overflow')
    state['generations'][role] = generation
    state['circuit_until'][role] = 0
    active = dict(generation=generation, ticket=request_id,
        phase='authorized', owner=None, ready_at=None,
        controller_generation=state['generations']['controller'])
    state['active'][role] = active
    return dict(allowed=True, role=role, grant=deepcopy(active))


def record_fault(state, args, now):
    keys(args, ('generation', 'fingerprint', 'unknown'))
    require(integer(args['generation'], minimum=1) and
            type(args['fingerprint']) is str and
            HASH.fullmatch(args['fingerprint']) is not None and
            type(args['unknown']) is bool, 'invalid model fault')
    if args['generation'] != state['generations']['model']:
        return denied('stale_generation')
    failures = state['failures']
    if any(item['generation'] == args['generation'] and
           item['fingerprint'] == args['fingerprint'] and
           item['unknown'] == args['unknown'] for item in failures):
        return dict(recorded=False, reason='duplicate_fault')
    if len(failures) >= MAX_FAILURES:
        state['quarantine'] = 'failure_history_full'
        return denied('quarantined')
    failures.append(dict(args, at=now))
    same = {item['generation'] for item in failures
            if item['fingerprint'] == args['fingerprint']}
    unknown = {item['generation'] for item in failures if item['unknown']}
    if len(same) >= 2:
        state['quarantine'] = 'repeated_model_fault'
    elif len(unknown) >= 2:
        state['quarantine'] = 'unknown_exit_repeated'
    count = len([at for at in state['attempts']['model']
                 if now-at < 30*MINUTE])
    state['not_before']['model'] = max(state['not_before']['model'],
                                     now+(5, 20, 60)[min(count, 2)]*SECOND)
    return dict(recorded=True, quarantine=state['quarantine'])


def apply(state, operation, args, now, request_id):
    if operation == 'desired':
        keys(args, ('state',))
        require(args['state'] in tuple(item.value for item in Desired),
                'invalid desired state')
        state['desired'] = args['state']
        return dict(desired=state['desired'])
    if operation == 'authorize':
        return authorize(state, args, now, request_id)
    if operation in ('spawned', 'ready', 'retired'):
        extra = ('owner',) if operation == 'spawned' else ('verified',)
        keys(args, ('role', 'ticket', *extra))
        active = active_grant(state, args)
        if operation == 'spawned':
            require(active['phase'] == 'authorized', 'grant already consumed')
            owner_valid(args['owner'])
            require(args['owner']['boot_id'] == state['clock']['boot_id'],
                    'owner boot mismatch')
            active.update(phase='spawned', owner=deepcopy(args['owner']))
        else:
            require(args['verified'] is True, 'positive verification required')
            if operation == 'ready':
                require(active['phase'] == 'spawned', 'not newly spawned')
                require(state['desired'] == Desired.RUNNING and
                        state['quarantine'] is None, 'admission is closed')
                active['phase'] = 'ready'
                active['ready_at'] = now
            else:
                if args['role'] == 'controller':
                    require(state['active']['model'] is None,
                            'model subtree not retired')
                state['active'][args['role']] = None
        return dict(recorded=True)
    if operation == 'fault':
        return record_fault(state, args, now)
    if operation == 'stable':
        keys(args, ('role', 'ticket', 'successful_request'))
        require(args['role'] == 'model', 'only model stability closes incident')
        active = active_grant(state, args)
        require(active['phase'] == 'ready' and
                args['successful_request'] is True and
                now-active['ready_at'] >= 10*MINUTE and
                state['desired'] == Desired.RUNNING and
                state['quarantine'] is None,
                'model stability not established')
        state['failures'] = []
        return dict(incident_closed=True, budgets_preserved=True)
    if operation == 'quarantine':
        keys(args, ('reason',))
        require(type(args['reason']) is str and args['reason'] in QUARANTINES,
                'invalid quarantine reason')
        # Protective facts are sticky and cannot be replaced by a lesser
        # observation/timeout. Preserve the first reason for reconciliation.
        if state['quarantine'] is None:
            state['quarantine'] = args['reason']
        return dict(quarantine=state['quarantine'])
    if operation == 'acknowledge':
        keys(args, ('reason',))
        require(state['desired'] == Desired.STOPPED and
                all(active is None for active in state['active'].values()),
                'explicit acknowledgement requires stopped empty service')
        require(type(args['reason']) is str and args['reason'] in QUARANTINES and
                args['reason'] == state['quarantine'],
                'quarantine reason changed')
        state['quarantine'] = None
        return dict(acknowledged=True)
    raise LedgerError('unsupported transaction')


def transition(current, request, stamp):
    """Return a new state/receipt; caller must commit before returning a grant.

    Revision prevents old requests being replayed after the one bounded cached
    receipt. Retrying the last request returns the same ticket, not a new start;
    Guardian must reconcile it and never spawn twice for one ticket.
    """
    validate(current, current['service_id'])
    keys(request, ('id', 'expected_revision', 'operation', 'args'))
    require(identifier(request['id']) and
            integer(request['expected_revision']) and
            type(request['operation']) is str and type(request['args']) is dict,
            'invalid transaction envelope')
    require(len(canonical(request)) <= 4096, 'transaction size limit')
    fingerprint = digest(request)
    last = current['last']
    if last is not None and request['id'] == last['id']:
        require(last['digest'] == fingerprint, 'transaction id reused')
        return deepcopy(current), deepcopy(last['reply'])
    require(request['expected_revision'] == current['revision'],
            'stale ledger revision')
    state = deepcopy(current)
    now = tick(state, stamp)
    result = apply(state, request['operation'], request['args'], now,
                   request['id'])
    state['revision'] += 1
    require(integer(state['revision']), 'revision overflow')
    reply = dict(revision=state['revision'], result=result)
    state['last'] = dict(id=request['id'], digest=fingerprint, reply=reply)
    validate(state, state['service_id'])
    return state, deepcopy(reply)
