"""Private version intent journal for the recovery root's isolated I/O worker.

Not exposed through payload controller or ingress commands. Bootstrap belongs
to an explicit stopped registration transaction, never a runtime fallback.
"""
import copy

from tools.serve_recovery import access_store, ledger, version_policy


class Store(access_store.Store):
    contract = version_policy
    format = 'q4t-version-selection-store-v1'
    marker_name = 'version-selection.json'
    binding_format = 'q4t-version-selection-binding-v1'
    directory_name = 'version-selection'


def open_bound(binding):
    return access_store.open_bound(binding, Store)


def initialize_bound(binding, initial):
    return access_store.initialize_bound(binding, initial, Store)


def execute(binding, request):
    op = request.get('op')
    ledger.require(op in ('load', 'commit'), 'invalid version operation')
    ledger.keys(request, ('sequence', 'op') +
        (('request_id', 'expected_revision', 'candidate') if op == 'commit' else ()))
    ledger.require(ledger.integer(request['sequence'], minimum=1),
                   'invalid version operation sequence')
    with open_bound(binding) as writer:
        if op == 'commit':
            writer.transact(request['request_id'], request['expected_revision'], request['candidate'])
        return copy.deepcopy(writer.head['state']['policy'])
