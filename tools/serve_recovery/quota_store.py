"""Independent durable limits journal using the credential transaction engine.

Policy settings only. Runtime minute usage and its crash uncertainty are a
separate recovery contract; a policy receipt does not claim usage durability.
"""
from tools.serve_recovery import access_store, quota_policy


class Store(access_store.Store):
    contract = quota_policy
    format = 'q4t-client-limits-store-v1'
    marker_name = 'client-limits.json'
    binding_format = 'q4t-client-limits-binding-v1'
    directory_name = 'client-limits'


def check_binding(value):
    return access_store.check_binding(value, Store)


def open_bound(value):
    return access_store.open_bound(value, Store)


def initialize_bound(value, policy):
    return access_store.initialize_bound(value, policy, Store)
