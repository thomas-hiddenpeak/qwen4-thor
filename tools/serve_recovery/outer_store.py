"""Explicit outer recovery journal, separate from the live Guardian event loop.

A committed charge is spent even if its response is lost. An old receipt does
not authorize another fork. State loss never recreates a fresh restart budget.
The original core journal continues to own intent, faults and model budgets.
"""
from copy import deepcopy
import uuid

from tools.serve_recovery import access_store, ledger, outer_policy, store


class Store(access_store.Store):
    contract = outer_policy
    format = 'q4t-outer-recovery-store-v1'
    marker_name = 'outer-recovery.json'
    binding_format = 'q4t-outer-recovery-binding-v1'
    directory_name = 'outer-recovery'


def open_bound(binding):
    writer = access_store.open_bound(binding, Store)
    try:
        ledger.require(writer.head['state']['policy']['service_id'] == binding['service_id'],
                       'outer journal belongs to another service')
        return writer
    except BaseException:
        writer.close()
        raise


def initialize_bound(binding, initial):
    ledger.require(initial['service_id'] == binding['service_id'],
                   'outer policy belongs to another service')
    return access_store.initialize_bound(binding, initial, Store)


def commit_once(binding, operation, args, *, clock=store.stamp):
    with open_bound(binding) as writer:
        previous = writer.head['state']['policy']
        candidate = outer_policy.change(previous, operation, args, clock())
        receipt = writer.transact(str(uuid.uuid4()), previous['revision'], candidate)
        ledger.require(not receipt['replayed'], 'outer attempt receipt cannot be dispatched twice')
        return dict(state=deepcopy(candidate), receipt=receipt,
                    receipt_sha256=ledger.digest(receipt))
