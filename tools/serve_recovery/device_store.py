"""Privileged device-domain journal; never run in the model or Guardian loop.

Reuse the existing checksummed, single-writer, recoverable atomic storage.
An old or lost receipt NEVER regenerates an admission pipe: every subsequent
issuance requires a fresh durable charge. Normal service journals are separate
and continue to enforce the original model/role budget and stop intent.
"""
import copy
import os
import uuid

from tools.serve_recovery import access_store, device_budget, ledger, store


class Store(access_store.Store):
    contract = device_budget
    format = 'q4t-device-domain-store-v1'
    marker_name = 'device-domain.json'
    binding_format = 'q4t-device-domain-binding-v1'
    directory_name = 'device-domain'


def privileged():
    ledger.require(os.geteuid() == 0, 'device domain management requires root')


def open_bound(binding):
    privileged()
    writer = access_store.open_bound(binding, Store)
    try:
        ledger.require(writer.head['state']['policy']['service_id'] == binding['service_id'],
                       'device journal belongs to another service')
        return writer
    except BaseException:
        writer.close()
        raise


def initialize_bound(binding, initial):
    privileged()
    ledger.require(initial['service_id'] == binding['service_id'],
                   'device budget belongs to another service')
    return access_store.initialize_bound(binding, initial, Store)


def commit_qualification(binding, qualification_sha256, *, clock=store.stamp):
    with open_bound(binding) as writer:
        previous = writer.head['state']['policy']
        candidate = device_budget.qualify(previous, qualification_sha256, clock())
        receipt = writer.transact(str(uuid.uuid4()), previous['revision'], candidate)
        ledger.require(not receipt['replayed'], 'device qualification receipt replay')
        return copy.deepcopy(candidate)


def disable(binding, *, clock=store.stamp):
    with open_bound(binding) as writer:
        previous = writer.head['state']['policy']
        candidate = device_budget.disable(previous, clock())
        writer.transact(str(uuid.uuid4()), previous['revision'], candidate)
        return copy.deepcopy(candidate)


def charge_once(binding, manifest_sha256, lease_device, lease_inode, *, clock=store.stamp):
    with open_bound(binding) as writer:
        previous = writer.head['state']['policy']
        request_id = str(uuid.uuid4())
        candidate = device_budget.charge(previous, manifest_sha256, lease_device,
                                         lease_inode, request_id, clock())
        receipt = writer.transact(request_id, previous['revision'], candidate)
        ledger.require(not receipt['replayed'], 'cannot reissue a spent cold admission')
        return dict(state=copy.deepcopy(candidate), receipt=receipt,
                    receipt_sha256=ledger.digest(receipt))
