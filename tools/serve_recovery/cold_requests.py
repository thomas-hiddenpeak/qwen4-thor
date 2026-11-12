"""Root-owned single issuance per original outer attempt, across host upgrades.

This is a replay fence, not another restart budget. Consuming an attempt is
durable before device charging or issuing any kernel token. Lost replies never
reissue; the original outer and device journals retain their independent limits.
"""
from copy import deepcopy
import uuid

from tools.serve_recovery import access_store, device_budget, ledger

FORMAT = 'q4t-cold-outer-requests-v1'
MAX_BYTES = 128*1024
digest = ledger.digest
revision = ledger.integer


def request(value):
    ledger.keys(value, ('version', 'host_id', 'service_id', 'manifest_sha256',
        'ticket', 'serial', 'boot_id', 'custody', 'lease_device', 'lease_inode'))
    from tools.serve_recovery import outer_policy
    ledger.require(type(value['version']) is int and value['version'] == 1 and
        all(ledger.identifier(value[n]) for n in ('host_id', 'service_id', 'ticket', 'boot_id')) and
        all(ledger.integer(value[n], minimum=1) for n in ('serial', 'lease_device', 'lease_inode')),
        'invalid cold outer request')
    device_budget.hashed(value['manifest_sha256'])
    outer_policy.custody_valid(value['custody'], value['boot_id'])
    return value


def initial(service_id, config_sha256):
    return validate(dict(format=FORMAT, service_id=service_id,
        configuration_sha256=config_sha256, revision=0, last_serial=0, last=None))


def validate(value):
    ledger.keys(value, ('format', 'service_id', 'configuration_sha256', 'revision', 'last_serial', 'last'))
    ledger.require(value['format'] == FORMAT and ledger.identifier(value['service_id']) and
        revision(value['revision']) and revision(value['last_serial']) and
        value['last_serial'] >= value['revision'] and
        (value['revision'] == 0) == (value['last_serial'] == 0) == (value['last'] is None),
        'invalid cold request history')
    device_budget.hashed(value['configuration_sha256'])
    if value['last'] is not None:
        selected = request(value['last'])
        ledger.require(selected['service_id'] == value['service_id'] and
            selected['serial'] == value['last_serial'], 'cold request history binding differs')
    return value


def consume(previous, selected):
    value = deepcopy(validate(previous)); request(selected)
    ledger.require(selected['service_id'] == value['service_id'] and
        selected['serial'] > value['last_serial'], 'cold outer attempt was already spent or regressed')
    value.update(revision=value['revision']+1, last_serial=selected['serial'], last=deepcopy(selected))
    return validate(value)


def transition(previous, candidate):
    validate(previous); validate(candidate)
    ledger.require(candidate == consume(previous, candidate['last']), 'cold replay fence transition differs')
    return candidate


def describe(value):
    validate(value)
    return dict(revision=value['revision'], last_outer_serial=value['last_serial'],
                launch_authorized=False, model_actions=False)


class Store(access_store.Store):
    format = 'q4t-cold-outer-requests-store-v1'
    marker_name = 'cold-outer-requests.json'
    binding_format = 'q4t-cold-outer-requests-binding-v1'
    directory_name = 'cold-outer-requests'


# The storage adapter deliberately reuses the existing journal implementation.
import sys
Store.contract = sys.modules[__name__]


def initialize(directory, config, *, first):
    binding = access_store.binding(directory, config['service_id'])
    seed = initial(config['service_id'], ledger.digest(config))
    if first: access_store.bootstrap_bound(binding, seed, Store)
    with access_store.open_bound(binding, Store) as writer:
        current = validate(writer.head['state']['policy'])
        ledger.require(current['configuration_sha256'] == ledger.digest(config) and
            current['service_id'] == config['service_id'], 'cold request installation differs')
        if first: ledger.require(current == seed, 'unpublished cold replay fence was already used')


def spend(directory, config, selected):
    binding = access_store.binding(directory, config['service_id'])
    with access_store.open_bound(binding, Store) as writer:
        previous = validate(writer.head['state']['policy'])
        ledger.require(previous['configuration_sha256'] == ledger.digest(config),
                       'cold request configuration changed')
        candidate = consume(previous, selected)
        receipt = writer.transact(str(uuid.uuid4()), previous['revision'], candidate)
        ledger.require(not receipt['replayed'], 'cannot reissue a cold attempt receipt')
        return dict(request_sha256=ledger.digest(selected), receipt_sha256=ledger.digest(receipt),
                    revision=candidate['revision'], serial=candidate['last_serial'])
