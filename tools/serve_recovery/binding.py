"""Immutable model-generation binding and per-process gateway credentials."""
import hashlib

from tools.serve_gateway import client_policy as cp
from tools.serve_recovery import ledger


FIELDS = ('port', 'operations_port', 'gateway_token', 'client_policy_sha256',
          'binary_sha256', 'protocol_sha256')


def policy_bytes(base, token):
    """Only the credential changes; quotas and fairness remain identical."""
    value = dict(cp.validate(base), gateway_token=token)
    cp.validate(value)
    return ledger.canonical(value)+b'\n'


def quota_enforced(base):
    cp.validate(base)
    return any(c.get('requests_per_minute', 0) or c.get('tokens_per_minute', 0)
               for c in base['clients'])


def selected_profile(value, profiles):
    digest = value.get('capacity_sha256')
    ledger.require(type(digest) is str and ledger.HASH.fullmatch(digest),
                   'missing qualified generation profile')
    selected = [p for p in profiles.values() if ledger.digest(p) == digest]
    ledger.require(len(selected) == 1, 'generation profile is not qualified')
    return selected[0]


def validate(value, *, base, port, operations_port, binary, protocol, profiles=None):
    ledger.keys(value, FIELDS + (('capacity_sha256',) if profiles is not None else ()))
    if profiles is not None: selected_profile(value, profiles)
    ledger.require(type(value['port']) is int and value['port'] == port and
                   type(value['operations_port']) is int and
                   value['operations_port'] == operations_port and
                   value['binary_sha256'] == binary and
                   value['protocol_sha256'] == protocol,
                   'generation release/endpoint mismatch')
    for key in FIELDS[2:]:
        ledger.require(type(value[key]) is str and
                       ledger.HASH.fullmatch(value[key]),
                       'invalid generation digest/credential')
    raw = policy_bytes(base, value['gateway_token'])
    ledger.require(hashlib.sha256(raw).hexdigest() == value['client_policy_sha256'],
                   'generation policy changed beyond credential rotation')
    return cp.binding(raw)
