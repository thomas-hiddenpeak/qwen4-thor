"""Native credential policy contract. Only fingerprints cross control boundaries."""
import copy
import hashlib
import re

from tools.serve_recovery import ledger

MAX_CLIENTS = 64
MAX_KEYS = 4
MAX_HISTORY = 4096
MAX_BYTES = 1024 * 1024
MAX_REVISION = 2**53 - 1
CLIENT = re.compile(r'[a-z][a-z0-9_-]{0,31}\Z')
KEY = re.compile(r'[A-Za-z0-9_-]{32,128}\Z')


def revision(value, *, minimum=0):
    return ledger.integer(value, minimum=minimum) and value <= MAX_REVISION


def key_digest(value):
    ledger.require(type(value) is str and KEY.fullmatch(value), 'invalid API key')
    return hashlib.sha256(value.encode()).hexdigest()


def validate(value):
    ledger.keys(value, ('version', 'revision', 'clients', 'key_owners'))
    ledger.require(type(value['version']) is int and value['version'] == 1 and
        revision(value['revision']), 'invalid access revision')
    clients, history = value['clients'], value['key_owners']
    ledger.require(type(clients) is list and 1 <= len(clients) <= MAX_CLIENTS and
        type(history) is dict and len(history) <= MAX_HISTORY, 'access capacity exceeded')
    ids, active = set(), set()
    for row in clients:
        ledger.keys(row, ('id', 'key_sha256'))
        client, keys = row['id'], row['key_sha256']
        ledger.require(type(client) is str and CLIENT.fullmatch(client) and
            client not in ids and type(keys) is list and len(keys) <= MAX_KEYS,
            'invalid access client')
        ids.add(client)
        for key in keys:
            ledger.require(type(key) is str and ledger.HASH.fullmatch(key) and
                key not in active and history.get(key) == client, 'invalid key ownership')
            active.add(key)
    ledger.require(all(type(k) is str and ledger.HASH.fullmatch(k) and
        type(v) is str and v in ids for k, v in history.items()), 'invalid access history')
    ledger.require(len(ledger.canonical(value)) <= MAX_BYTES, 'access policy byte bound')
    return value


def digest(value):
    validate(value)
    raw = 'q4t-client-access-v1\n' + str(value['revision']) + '\n'
    for row in value['clients']:
        raw += row['id'] + ':' + ''.join(k + ',' for k in sorted(row['key_sha256'])) + '\n'
    raw += 'history\n'
    raw += ''.join(k + ':' + v + '\n' for k, v in sorted(value['key_owners'].items()))
    return hashlib.sha256(raw.encode()).hexdigest()


def transition(previous, candidate):
    validate(previous); validate(candidate)
    ledger.require(previous['revision'] < MAX_REVISION and
        candidate['revision'] == previous['revision'] + 1, 'access revision conflict')
    before, after = previous['clients'], candidate['clients']
    ledger.require(len(after) >= len(before) and
        [c['id'] for c in after[:len(before)]] == [c['id'] for c in before],
        'client slots cannot be removed or reassigned')
    ledger.require(all(candidate['key_owners'].get(k) == v for k, v in
        previous['key_owners'].items()), 'historical key ownership changed')
    return candidate


def from_keys(rows, previous=None):
    """Full desired client list; omitted slots are refused, empty keys revoke."""
    ledger.require(type(rows) is list and 1 <= len(rows) <= MAX_CLIENTS,
                   'access client count')
    if previous is not None:
        validate(previous)
    value = dict(version=1, revision=0 if previous is None else previous['revision'] + 1,
        clients=[], key_owners={} if previous is None else copy.deepcopy(previous['key_owners']))
    for row in rows:
        ledger.keys(row, ('id', 'api_keys'))
        ledger.require(type(row['id']) is str and CLIENT.fullmatch(row['id']) and
            type(row['api_keys']) is list and len(row['api_keys']) <= MAX_KEYS,
            'invalid access client')
        keys = [key_digest(key) for key in row['api_keys']]
        for key in keys:
            ledger.require(value['key_owners'].get(key, row['id']) == row['id'],
                           'historical key ownership changed')
            value['key_owners'][key] = row['id']
        value['clients'].append(dict(id=row['id'], key_sha256=keys))
    validate(value)
    return transition(previous, value) if previous is not None else value


def describe(value):
    validate(value)
    return dict(configured=True, revision=value['revision'], sha256=digest(value),
        clients=len(value['clients']), active_keys=sum(len(c['key_sha256']) for c in value['clients']),
        revoked_clients=sum(not c['key_sha256'] for c in value['clients']),
        history_keys=len(value['key_owners']), credential_values_redacted=True)
