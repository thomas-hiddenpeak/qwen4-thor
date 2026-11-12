"""Durable auxiliary restart limit; grants no device/model authority."""
from copy import deepcopy
import sys
import uuid

from tools.serve_recovery import access_store, ledger, store

FORMAT = 'q4t-cold-issuer-recovery-v1'
MAX_BYTES = 65536
WINDOW = 15*ledger.MINUTE
COOLDOWN = 5*ledger.SECOND
MAX_STARTS = 3
digest = ledger.digest
revision = ledger.integer


def initial(config, stamp):
    return validate(dict(format=FORMAT, service_id=config['service_id'],
        configuration_sha256=ledger.digest(config), revision=0, total=0,
        clock=dict(boot_id=stamp.boot_id, monotonic_ns=stamp.monotonic_ns, logical_ns=0), starts=[]))


def validate(value):
    ledger.keys(value, ('format', 'service_id', 'configuration_sha256', 'revision', 'total', 'clock', 'starts'))
    ledger.require(value['format'] == FORMAT and ledger.identifier(value['service_id']) and
        type(value['configuration_sha256']) is str and ledger.HASH.fullmatch(value['configuration_sha256']) and
        revision(value['revision']) and revision(value['total']) and value['total'] <= value['revision'], 'invalid auxiliary recovery history')
    clock = value['clock']; ledger.keys(clock, ('boot_id', 'monotonic_ns', 'logical_ns'))
    ledger.Stamp(clock['boot_id'], clock['monotonic_ns'])
    ledger.require(revision(clock['logical_ns']) and type(value['starts']) is list and
                   len(value['starts']) <= MAX_STARTS, 'invalid auxiliary clock or bound')
    prior = None
    for entry in value['starts']:
        ledger.keys(entry, ('serial', 'at', 'ticket'))
        ledger.require(revision(entry['serial'], minimum=1) and entry['serial'] <= value['total'] and
            revision(entry['at']) and 0 <= clock['logical_ns']-entry['at'] < WINDOW and
            ledger.identifier(entry['ticket']), 'invalid auxiliary start')
        if prior is not None:
            ledger.require(entry['serial'] == prior['serial']+1 and entry['at']-prior['at'] >= COOLDOWN and
                           entry['ticket'] != prior['ticket'], 'auxiliary start history differs')
        prior = entry
    ledger.require((value['total'] == 0) == (not value['starts']) and
        (not value['starts'] or value['starts'][-1]['serial'] == value['total']), 'lost auxiliary start history')
    return value


def advance(previous, stamp):
    value = deepcopy(validate(previous)); clock = value['clock']; elapsed = 0
    if stamp.boot_id == clock['boot_id']:
        ledger.require(stamp.monotonic_ns >= clock['monotonic_ns'], 'auxiliary clock regressed')
        elapsed = stamp.monotonic_ns-clock['monotonic_ns']
    now = clock['logical_ns']+elapsed
    clock.update(boot_id=stamp.boot_id, monotonic_ns=stamp.monotonic_ns, logical_ns=now)
    value['starts'] = [s for s in value['starts'] if now-s['at'] < WINDOW]
    return value


def decision(previous, stamp):
    value = advance(previous, stamp); starts = value['starts']; now = value['clock']['logical_ns']
    wait = max(0, starts[-1]['at']+COOLDOWN-now) if starts else 0
    if len(starts) >= MAX_STARTS: wait = max(wait, starts[0]['at']+WINDOW-now)
    return dict(allowed=wait == 0, retry_after_seconds=wait/ledger.SECOND,
        reason='auxiliary_start_limit' if len(starts) >= MAX_STARTS else 'auxiliary_cooldown' if wait else 'ready',
        total=previous['total'], model_actions=False)


def charge(previous, stamp, ticket):
    ledger.require(ledger.identifier(ticket) and decision(previous, stamp)['allowed'], 'auxiliary restart budget')
    value = advance(previous, stamp)
    ledger.require(all(s['ticket'] != ticket for s in previous['starts']), 'auxiliary ticket reused')
    value['revision'] += 1; value['total'] += 1
    value['starts'].append(dict(serial=value['total'], at=value['clock']['logical_ns'], ticket=ticket))
    return validate(value)


def observe_boot(previous, stamp):
    ledger.require(stamp.boot_id != previous['clock']['boot_id'], 'auxiliary boot is unchanged')
    value = advance(previous, stamp); value['revision'] += 1
    return validate(value)


def transition(previous, candidate):
    validate(candidate); clock = candidate['clock']; stamp = ledger.Stamp(clock['boot_id'], clock['monotonic_ns'])
    expected = observe_boot(previous, stamp) if candidate['total'] == previous['total'] else \
        charge(previous, stamp, candidate['starts'][-1]['ticket'])
    ledger.require(candidate == expected, 'auxiliary journal transition differs')
    return candidate


def describe(value):
    validate(value)
    return dict(total=value['total'], model_actions=False)


class Store(access_store.Store):
    format = 'q4t-cold-issuer-recovery-store-v1'
    marker_name = 'cold-issuer-recovery.json'
    binding_format = 'q4t-cold-issuer-recovery-binding-v1'
    directory_name = 'cold-issuer-recovery'


Store.contract = sys.modules[__name__]


def initialize(directory, config, *, first):
    binding = access_store.binding(directory, config['service_id'])
    if first:
        from tools.serve_recovery import device_domain, device_resource
        fence = device_domain.read_record(directory/device_resource.FENCE)
        seed = initial(config, ledger.Stamp(fence['boot_id'], 0))
        access_store.bootstrap_bound(binding, seed, Store)
    with access_store.open_bound(binding, Store) as writer:
        value = validate(writer.head['state']['policy'])
        ledger.require(value['configuration_sha256'] == ledger.digest(config), 'auxiliary installation differs')


def attempt(directory, config):
    binding = access_store.binding(directory, config['service_id'])
    with access_store.open_bound(binding, Store) as writer:
        previous = validate(writer.head['state']['policy']); stamp = store.stamp()
        ledger.require(previous['configuration_sha256'] == ledger.digest(config), 'auxiliary configuration differs')
        if stamp.boot_id != previous['clock']['boot_id']:
            # Persist the new monotonic origin even while cooldown denies a
            # launch. Repeated observations must make progress without credit
            # for time spent powered off or clearing the existing history.
            candidate = observe_boot(previous, stamp)
            writer.transact(str(uuid.uuid4()), previous['revision'], candidate)
            previous = candidate
        result = decision(previous, stamp)
        if result['allowed']:
            candidate = charge(previous, stamp, str(uuid.uuid4()))
            receipt = writer.transact(str(uuid.uuid4()), previous['revision'], candidate)
            ledger.require(not receipt['replayed'], 'auxiliary launch cannot replay a receipt')
            result.update(total=candidate['total'], ticket=candidate['starts'][-1]['ticket'])
        return result
