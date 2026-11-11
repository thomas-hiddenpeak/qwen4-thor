"""Bounded outer-Guardian recovery intent; never a model or device authority.

Only an explicitly enabled outer adapter may ask to restart a Guardian. It must
hold the service lifecycle locks, reconcile the old tree and read the original
core journal. This pure policy preserves that journal's stop/quarantine intent.
Runtime proof collection and actual process dispatch belong to the adapter.
"""
from copy import deepcopy

from tools.serve_recovery import ledger

FORMAT = 'q4t-outer-recovery-v1'
MAX_BYTES = 128*1024
WINDOW = 15*ledger.MINUTE
MAX_STARTS = 3
COOLDOWN = 30*ledger.SECOND
digest = ledger.digest
revision = ledger.integer


def initial(service_id, stamp):
    return validate(dict(format=FORMAT, revision=0, service_id=service_id,
        enabled=False, clock=dict(boot_id=stamp.boot_id,
            monotonic_ns=stamp.monotonic_ns, logical_ns=0), total=0,
        starts=[], pending=None, protected=False, last=None))


def hashed(value):
    ledger.require(type(value) is str and ledger.HASH.fullmatch(value),
                   'invalid outer recovery digest')


def custody_valid(value, boot_id):
    ledger.keys(value, ('supervisor', 'instance_token', 'tree_scope'))
    ledger.owner_valid(value['supervisor'])
    ledger.require(value['supervisor']['boot_id'] == boot_id and
        ledger.identifier(value['instance_token']) and ledger.identifier(value['tree_scope']),
        'invalid original outer custody')


def pending_valid(pending):
    ledger.keys(pending, ('ticket', 'serial', 'boot_id', 'manifest_sha256',
                         'core_sha256', 'core_revision', 'core_generations', 'custody', 'owner'))
    ledger.require(ledger.identifier(pending['ticket']) and
        revision(pending['serial'], minimum=1) and revision(pending['core_revision']) and
        ledger.identifier(pending['boot_id']), 'invalid pending outer attempt')
    hashed(pending['manifest_sha256']); hashed(pending['core_sha256'])
    ledger.keys(pending['core_generations'], ledger.ROLES)
    ledger.require(all(revision(n) for n in pending['core_generations'].values()),
                   'invalid original core generations')
    custody_valid(pending['custody'], pending['boot_id'])
    if pending['owner'] is not None:
        ledger.owner_valid(pending['owner'])
        ledger.require(pending['owner']['boot_id'] == pending['boot_id'],
                       'outer owner belongs to another boot')
    return pending


def validate(value):
    ledger.keys(value, ('format', 'revision', 'service_id', 'enabled', 'clock',
                       'total', 'starts', 'pending', 'protected', 'last'))
    ledger.require(value['format'] == FORMAT and revision(value['revision']) and
        ledger.identifier(value['service_id']) and type(value['enabled']) is bool and
        type(value['protected']) is bool and revision(value['total']) and
        value['total'] <= value['revision'], 'invalid outer recovery state')
    clock = value['clock']
    ledger.keys(clock, ('boot_id', 'monotonic_ns', 'logical_ns'))
    ledger.Stamp(clock['boot_id'], clock['monotonic_ns'])
    ledger.require(revision(clock['logical_ns']), 'invalid outer logical clock')
    starts = value['starts']
    ledger.require(type(starts) is list and len(starts) <= MAX_STARTS,
                   'outer start history bound')
    prior = None
    for entry in starts:
        ledger.keys(entry, ('serial', 'at', 'ticket'))
        ledger.require(revision(entry['serial'], minimum=1) and
            entry['serial'] <= value['total'] and revision(entry['at']) and
            0 <= clock['logical_ns']-entry['at'] < WINDOW and
            ledger.identifier(entry['ticket']), 'invalid outer start history')
        if prior is not None:
            ledger.require(entry['serial'] == prior['serial']+1 and
                entry['at']-prior['at'] >= COOLDOWN, 'nonconsecutive outer starts')
        prior = entry
    if starts:
        ledger.require(starts[-1]['serial'] == value['total'], 'lost outer start history')
    pending = value['pending']
    if pending is not None:
        pending_valid(pending)
        ledger.require(pending['serial'] == value['total'], 'outer attempt serial changed')
    last = value['last']
    ledger.require((last is None) == (value['revision'] == 0), 'missing outer transition')
    if last is not None:
        ledger.keys(last, ('operation', 'args', 'result'))
        ledger.require(last['operation'] in ('configure', 'request', 'spawned', 'retired', 'acknowledge') and
            type(last['args']) is dict and type(last['result']) is dict,
            'invalid outer transition envelope')
    ledger.require(len(ledger.canonical(value)) <= MAX_BYTES, 'outer policy size bound')
    return value


def tick(value, stamp):
    clock = value['clock']; elapsed = 0
    if stamp.boot_id == clock['boot_id']:
        ledger.require(stamp.monotonic_ns >= clock['monotonic_ns'],
                       'outer monotonic clock moved backwards')
        elapsed = stamp.monotonic_ns-clock['monotonic_ns']
    # Neither reboot nor wall clock creates a new budget or clears protection.
    now = clock['logical_ns']+elapsed
    ledger.require(revision(now), 'outer logical clock overflow')
    clock.update(boot_id=stamp.boot_id, monotonic_ns=stamp.monotonic_ns, logical_ns=now)
    value['starts'] = [entry for entry in value['starts'] if now-entry['at'] < WINDOW]
    return now


def core_state(args, value):
    core = args['core']
    ledger.validate(core, value['service_id'])
    return core


def request(value, args, stamp, now):
    ledger.keys(args, ('core', 'ticket', 'manifest_sha256', 'cleanup_verified', 'identity_valid',
                       'custody'))
    core = core_state(args, value)
    ledger.require(ledger.identifier(args['ticket']) and
        type(args['cleanup_verified']) is bool and type(args['identity_valid']) is bool,
        'invalid outer start request')
    hashed(args['manifest_sha256'])
    custody_valid(args['custody'], stamp.boot_id)
    reason = None
    if not value['enabled']: reason = 'disabled'
    elif core['desired'] != 'RUNNING': reason = 'operator_intent'
    elif core['quarantine'] is not None: reason = 'core_protected'
    elif any(core['active'].values()) or value['pending'] is not None:
        reason = 'unreconciled_attempt'
    elif not args['cleanup_verified'] or not args['identity_valid']:
        reason = 'preflight_incomplete'
    elif value['protected']: reason = 'outer_protected'
    elif len(value['starts']) >= MAX_STARTS:
        value['protected'] = True
        reason = 'start_limit'
    elif value['starts'] and now-value['starts'][-1]['at'] < COOLDOWN:
        reason = 'cooldown'
    if reason is not None: return dict(allowed=False, reason=reason)
    ledger.require(all(entry['ticket'] != args['ticket'] for entry in value['starts']),
                   'outer start ticket reused')
    value['total'] += 1
    value['starts'].append(dict(serial=value['total'], at=now, ticket=args['ticket']))
    value['pending'] = dict(ticket=args['ticket'], serial=value['total'], boot_id=stamp.boot_id,
        manifest_sha256=args['manifest_sha256'], core_sha256=ledger.digest(core),
        core_revision=core['revision'], core_generations=deepcopy(core['generations']),
        custody=deepcopy(args['custody']), owner=None)
    return dict(allowed=True, ticket=args['ticket'], serial=value['total'])


def apply(value, operation, args, stamp, now):
    if operation == 'configure':
        ledger.keys(args, ('enabled',))
        ledger.require(type(args['enabled']) is bool, 'explicit outer enable flag required')
        value['enabled'] = args['enabled']
        return dict(enabled=value['enabled'], automatic_start=False)
    if operation == 'request': return request(value, args, stamp, now)
    if operation in ('spawned', 'retired'):
        ledger.keys(args, ('ticket', 'owner' if operation == 'spawned' else 'verified'))
        pending = value['pending']
        ledger.require(pending is not None and pending['ticket'] == args['ticket'],
                       'outer attempt identity changed')
        if operation == 'spawned':
            ledger.owner_valid(args['owner'])
            ledger.require(pending['owner'] is None and
                args['owner']['boot_id'] == pending['boot_id'] == stamp.boot_id,
                'outer attempt already dispatched or boot changed')
            pending['owner'] = deepcopy(args['owner'])
        else:
            ledger.require(args['verified'] is True, 'actual outer cleanup required')
            value['pending'] = None
        return dict(recorded=True)
    if operation == 'acknowledge':
        ledger.keys(args, ('core',))
        core = core_state(args, value)
        ledger.require(not value['enabled'] and value['protected'] and
            value['pending'] is None and core['desired'] == 'STOPPED' and
            not any(core['active'].values()), 'acknowledgement requires disabled stopped empty service')
        value['protected'] = False
        return dict(acknowledged=True, budget_preserved=True)
    raise ledger.LedgerError('unknown outer recovery transition')


def change(previous, operation, args, stamp):
    value = deepcopy(validate(previous)); now = tick(value, stamp)
    result = apply(value, operation, args, stamp, now)
    value['revision'] += 1
    value['last'] = dict(operation=operation, args=deepcopy(args), result=result)
    return validate(value)


def transition(previous, candidate):
    validate(previous); validate(candidate)
    last = candidate['last']; clock = candidate['clock']
    ledger.require(last is not None and candidate == change(previous, last['operation'],
        last['args'], ledger.Stamp(clock['boot_id'], clock['monotonic_ns'])),
        'outer transition changed protected state')
    return candidate


def describe(value):
    validate(value)
    return dict(format=FORMAT, enabled=value['enabled'], protected=value['protected'],
        charged_total=value['total'], recent_starts=len(value['starts']),
        unresolved_attempt=value['pending'] is not None, model_actions=False,
        device_authorized=False)
