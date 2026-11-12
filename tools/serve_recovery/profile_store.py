"""Private profile-intent journal. Its receipt never authorizes a model start."""
import copy

from tools.serve_recovery import access_store, ledger, profile_policy


class Store(access_store.Store):
    contract = profile_policy
    format = 'q4t-profile-selection-store-v1'
    marker_name = 'profile-selection.json'
    binding_format = 'q4t-profile-selection-binding-v1'
    directory_name = 'profile-selection'


def open_bound(binding):
    return access_store.open_bound(binding, Store)


def initialize_bound(binding, initial):
    return access_store.initialize_bound(binding, initial, Store)


def execute(binding, request):
    """Retry the exact durable transition, including after a lost receipt."""
    op = request.get('op')
    ledger.require(op in ('load', 'commit'), 'invalid profile operation')
    ledger.keys(request, ('sequence', 'op') +
                (('request_id', 'expected_revision', 'candidate')
                 if op == 'commit' else ()))
    ledger.require(ledger.integer(request['sequence'], minimum=1),
                   'invalid profile operation sequence')
    with open_bound(binding) as writer:
        if op == 'commit':
            writer.transact(request['request_id'], request['expected_revision'],
                            request['candidate'])
        return copy.deepcopy(writer.head['state']['policy'])
