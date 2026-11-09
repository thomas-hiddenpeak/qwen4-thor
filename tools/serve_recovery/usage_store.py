"""Usage handoff journal. File operations belong only in an owned I/O worker."""
import copy
import time
import uuid

from tools.serve_recovery import access_store, ledger, usage_policy as policy


class Store(access_store.Store):
    contract = policy
    format = 'q4t-client-usage-store-v1'
    marker_name = 'client-usage.json'
    binding_format = 'q4t-client-usage-binding-v1'
    directory_name = 'client-usage'


def open_bound(value):return access_store.open_bound(value, Store)


def initialize_bound(value):
    return access_store.initialize_bound(value, policy.initial(), Store)


def execute(binding, request, *, now_ns=None):
    """Generation-scoped idempotent operations, not operator counter resets."""
    operation = request.get('op')
    ledger.require(operation in ('load', 'begin', 'seal'), 'invalid usage operation')
    fields = ('sequence', 'op')
    if operation != 'load':fields += ('generation', 'owner')
    if operation == 'seal':fields += ('snapshot',)
    ledger.keys(request, fields)
    if operation != 'load':
        ledger.owner_valid(request['owner'])
        ledger.require(ledger.integer(request['generation'], minimum=1), 'invalid usage generation')
    if operation == 'seal':policy.snapshot(request['snapshot'])
    now = time.monotonic_ns() if now_ns is None else now_ns
    if operation != 'load':ledger.require(ledger.integer(now, minimum=1), 'invalid usage clock')
    with open_bound(binding) as writer:
        previous = writer.head['state']['policy']
        if operation == 'load':return copy.deepcopy(previous)
        generation, owner = request['generation'], request['owner']
        if (previous['generation'] == generation and previous['owner'] == owner and
                previous['phase'] == ('active' if operation == 'begin' else 'sealed')):
            if operation == 'seal':
                ledger.require(previous['snapshot'] == request['snapshot'], 'usage seal replay conflict')
            return copy.deepcopy(previous)
        if operation == 'begin':
            candidate = policy.begin(previous, generation, owner, now)
        else:
            ledger.require(int(request['snapshot']['captured_ns']) <= now, 'usage seal is in the future')
            candidate = policy.seal(previous, generation, owner, request['snapshot'])
        request_id = str(uuid.uuid5(uuid.UUID(binding['service_id']),
            'client-usage:' + str(generation) + ':' + operation))
        writer.transact(request_id, previous['revision'], candidate)
        return copy.deepcopy(writer.head['state']['policy'])
