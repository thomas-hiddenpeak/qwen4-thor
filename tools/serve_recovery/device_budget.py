"""Durable admission charges, independent of the original model restart budget.

The explicit privileged domain owner must qualify the actual kernel fence and
empty domain before calling this pure contract. This module neither proves
device availability nor issues a launch. A lost charge receipt is still spent;
only a new charge can produce another one-use kernel admission object.
"""
import copy

from tools.serve_recovery import ledger

FORMAT = 'q4t-device-admission-budget-v1'
MAX_BYTES = 128*1024
WINDOW = 30*ledger.MINUTE
MAX_WINDOW = 3
MAX_DAY = 6
COOLDOWN = 30*ledger.SECOND
LIFETIME = 300*ledger.SECOND
digest = ledger.digest
revision = ledger.integer


def hashed(value):
    ledger.require(type(value) is str and ledger.HASH.fullmatch(value),
                   'invalid device budget hash')


def initial(service_id, domain_id, runtime_uid, stamp):
    return validate(dict(format=FORMAT, revision=0, service_id=service_id,
        domain_id=domain_id, runtime_uid=runtime_uid,
        clock=dict(boot_id=stamp.boot_id, monotonic_ns=stamp.monotonic_ns,
                   logical_ns=0), qualification=None, total=0, attempts=[],
        last_ticket=None))


def validate(value):
    ledger.keys(value, ('format', 'revision', 'service_id', 'domain_id',
        'runtime_uid', 'clock', 'qualification', 'total', 'attempts', 'last_ticket'))
    ledger.require(value['format'] == FORMAT and revision(value['revision']) and
        ledger.identifier(value['service_id']) and ledger.identifier(value['domain_id']) and
        revision(value['runtime_uid'], minimum=1) and value['runtime_uid'] < 2**32-1 and
        revision(value['total']) and value['total'] <= value['revision'],
        'invalid device budget identity')
    clock = value['clock']
    ledger.keys(clock, ('boot_id', 'monotonic_ns', 'logical_ns'))
    ledger.Stamp(clock['boot_id'], clock['monotonic_ns'])
    ledger.require(revision(clock['logical_ns']), 'invalid device budget clock')
    qualification = value['qualification']
    if qualification is not None:
        ledger.keys(qualification, ('boot_id', 'sha256'))
        ledger.require(qualification['boot_id'] == clock['boot_id'],
                       'device qualification is from another boot')
        hashed(qualification['sha256'])
    attempts = value['attempts']
    ledger.require(type(attempts) is list and len(attempts) <= MAX_DAY,
                   'unbounded cold admission history')
    previous = None
    for attempt in attempts:
        ledger.keys(attempt, ('serial', 'at'))
        ledger.require(revision(attempt['serial'], minimum=1) and
            attempt['serial'] <= value['total'] and revision(attempt['at']) and
            attempt['at'] <= clock['logical_ns'] and
            clock['logical_ns']-attempt['at'] < ledger.DAY,
            'invalid cold admission history')
        if previous is not None:
            ledger.require(attempt['serial'] == previous['serial']+1 and
                attempt['at']-previous['at'] >= COOLDOWN,
                'nonconsecutive or rapid cold admission history')
        previous = attempt
    if attempts:
        ledger.require(attempts[-1]['serial'] == value['total'],
                       'cold admission history lost latest charge')
    ticket = value['last_ticket']
    ledger.require((ticket is None) == (value['total'] == 0),
                   'cold admission charge lacks receipt')
    if ticket is not None:
        ledger.keys(ticket, ('serial', 'id', 'boot_id', 'qualification_sha256',
            'manifest_sha256', 'lease_device', 'lease_inode', 'issued_ns', 'expires_ns'))
        ledger.require(ticket['serial'] == value['total'] and
            ledger.identifier(ticket['id']) and ledger.identifier(ticket['boot_id']) and
            revision(ticket['lease_device'], minimum=1) and
            revision(ticket['lease_inode'], minimum=1) and
            revision(ticket['issued_ns'], minimum=1) and revision(ticket['expires_ns']) and
            ticket['expires_ns']-ticket['issued_ns'] == LIFETIME,
            'invalid cold admission receipt')
        hashed(ticket['qualification_sha256']); hashed(ticket['manifest_sha256'])
    ledger.require(len(ledger.canonical(value)) <= 8192, 'device budget size')
    return value


def tick(previous, stamp):
    value = copy.deepcopy(validate(previous))
    clock = value['clock']
    elapsed = 0
    if stamp.boot_id == clock['boot_id']:
        ledger.require(stamp.monotonic_ns >= clock['monotonic_ns'],
                       'device monotonic clock moved backwards')
        elapsed = stamp.monotonic_ns-clock['monotonic_ns']
    else:
        # Reboot invalidates kernel custody and earns no missing-wall-time
        # credit. An actual new fence qualification is required before charge.
        value['qualification'] = None
    now = clock['logical_ns']+elapsed
    ledger.require(revision(now), 'device logical clock overflow')
    clock.update(boot_id=stamp.boot_id, monotonic_ns=stamp.monotonic_ns, logical_ns=now)
    value['attempts'] = [entry for entry in value['attempts']
                         if now-entry['at'] < ledger.DAY]
    value['revision'] += 1
    return value


def qualify(previous, qualification_sha256, stamp):
    hashed(qualification_sha256)
    value = tick(previous, stamp)
    value['qualification'] = dict(boot_id=stamp.boot_id, sha256=qualification_sha256)
    return validate(value)


def disable(previous, stamp):
    value = tick(previous, stamp)
    value['qualification'] = None
    return validate(value)


def charge(previous, manifest_sha256, lease_device, lease_inode, ticket_id, stamp):
    value = tick(previous, stamp)
    qualification = value['qualification']
    ledger.require(qualification is not None, 'current boot lacks device qualification')
    now = value['clock']['logical_ns']; attempts = value['attempts']
    ledger.require(not attempts or now-attempts[-1]['at'] >= COOLDOWN,
                   'cold admission cooldown')
    ledger.require(sum(now-entry['at'] < WINDOW for entry in attempts) < MAX_WINDOW and
                   len(attempts) < MAX_DAY, 'cold admission budget exhausted')
    ledger.require(value['last_ticket'] is None or
                   value['last_ticket']['id'] != ticket_id, 'cold admission ticket reused')
    value['total'] += 1
    attempts.append(dict(serial=value['total'], at=now))
    value['last_ticket'] = dict(serial=value['total'], id=ticket_id, boot_id=stamp.boot_id,
        qualification_sha256=qualification['sha256'], manifest_sha256=manifest_sha256,
        lease_device=lease_device, lease_inode=lease_inode,
        issued_ns=stamp.monotonic_ns, expires_ns=stamp.monotonic_ns+LIFETIME)
    return validate(value)


def transition(previous, candidate):
    """Reconstruct the entire allowed change; never reset an old charge."""
    validate(previous); validate(candidate)
    stamp = ledger.Stamp(candidate['clock']['boot_id'], candidate['clock']['monotonic_ns'])
    if candidate['total'] == previous['total']+1:
        ticket = candidate['last_ticket']
        expected = charge(previous, ticket['manifest_sha256'], ticket['lease_device'],
                          ticket['lease_inode'], ticket['id'], stamp)
    elif candidate['qualification'] is None:
        expected = disable(previous, stamp)
    else:
        expected = qualify(previous, candidate['qualification']['sha256'], stamp)
    ledger.require(candidate == expected, 'device budget transition changed protected state')
    return candidate


def describe(value):
    validate(value)
    return dict(format=FORMAT, revision=value['revision'], domain_id=value['domain_id'],
        qualified=value['qualification'] is not None, charged_total=value['total'],
        recent_charges=len(value['attempts']), model_actions=False,
        launch_authorized=False)
