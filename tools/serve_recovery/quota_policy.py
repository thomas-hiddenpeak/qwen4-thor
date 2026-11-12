"""Durable client limits, separated from per-model transport credentials."""
import copy

from tools.serve_gateway import client_policy as cp
from tools.serve_recovery import access_policy, ledger

# Policy is <= 64 KiB. Its bounded 128 transaction receipts need separate room.
MAX_BYTES = 128 * 1024
TOKEN = '0' * 64
revision = access_policy.revision


def validate(value):
    cp.validate(value)
    ledger.require(value['version'] in (3, 4) and value['gateway_token'] == TOKEN and
                   len(ledger.canonical(value)) <= cp.MAX_BYTES,
                   'invalid durable client limits')
    return value


def digest(value):
    return cp.digest(validate(value))


def from_base(base):
    cp.validate(base)
    value = dict(version=max(3, base['version']), revision=0, gateway_token=TOKEN,
                 fair_queue=base.get('fair_queue', False), clients=copy.deepcopy(base['clients']))
    for client in value['clients']:
        for name in cp.RATE_BOUNDS:client.setdefault(name, 0)
    return validate(value)


def transition(previous, candidate):
    validate(previous);validate(candidate)
    ledger.require(previous['revision'] < access_policy.MAX_REVISION and
        candidate['revision'] == previous['revision'] + 1, 'access revision conflict')
    before, after = previous['clients'], candidate['clients']
    ledger.require(len(after) >= len(before) and
        [c['id'] for c in after[:len(before)]] == [c['id'] for c in before],
        'client slots cannot be removed or reassigned')
    if candidate['version'] < previous['version']:
        ledger.require(all(a['max_file_body_bytes'] == b['max_body_bytes']
                           for a, b in zip(before, after)),
                       'file capacity must match legacy body limit')
    ledger.require(candidate['fair_queue'] == previous['fair_queue'],
                   'fairness mode requires coordinated scheduler configuration')
    return candidate


def desired(previous, clients, policy_version=None):
    value = dict(copy.deepcopy(validate(previous)), revision=previous['revision'] + 1,
                 clients=copy.deepcopy(clients))
    # New fields are an explicit operator choice. Existing snapshots retain
    # their exact version and semantic digest until that transaction occurs.
    if any(type(c) is dict and 'max_file_body_bytes' in c for c in clients):
        value['version'] = 4
    if policy_version is not None:
        ledger.require(type(policy_version) is int and policy_version in (3, 4),
                       'explicit client policy version must be 3 or 4')
        value['version'] = policy_version
    return transition(previous, value)


def describe(value):
    validate(value)
    return dict(configured=True, policy_version=value['version'],
        revision=value['revision'], sha256=digest(value),
        clients=len(value['clients']), fair_queue=value['fair_queue'],
        quota_clients=sum(bool(c['requests_per_minute'] or c['tokens_per_minute'])
                          for c in value['clients']), credential_values_redacted=True)


def capacity(value, inflight, queued):
    validate(value)
    ledger.require(ledger.integer(inflight, minimum=1) and ledger.integer(queued) and
        inflight <= 2**31 - 1 and queued <= 2**31 - 1, 'invalid global admission capacity')
    ledger.require(not value['fair_queue'] or (queued >= len(value['clients']) and
        sum(c['max_inflight'] for c in value['clients']) <= inflight),
        'client fairness exceeds global capacity')
    return value


def runtime(value, token):
    """Ephemeral generation token only; stable limits digest stays identical."""
    result = dict(copy.deepcopy(validate(value)), gateway_token=token)
    cp.validate(result)
    return result
