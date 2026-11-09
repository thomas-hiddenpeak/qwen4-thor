"""Durable usage handoff state, distinct from client policy and core authority.

An active generation consumes its predecessor's sealed snapshot durably before
it can be trusted for restoration. A crashed/uncertain generation is never a
sealed predecessor. Monotonic windows are portable only within the same boot.
"""
import copy
import re

from tools.serve_recovery import access_policy, ledger

MAX_BYTES = 128 * 1024
MAX_SNAPSHOT_BYTES = 48 * 1024
revision = access_policy.revision
COUNTS = ('requests', 'tokens', 'rpm_rejected', 'tpm_rejected', 'admitted', 'settled_tokens')


def decimal(value, *, maximum=2**64-1):
    ledger.require(type(value) is str and 1 <= len(value) <= 20 and
        re.fullmatch('0|[1-9][0-9]*', value) and int(value) <= maximum,
        'invalid usage integer')
    return int(value)


def snapshot(value):
    ledger.keys(value, ('version', 'captured_ns', 'policy_revision', 'policy_sha256', 'clients'))
    ledger.require(type(value['version']) is int and value['version'] == 1 and
        decimal(value['captured_ns'], maximum=ledger.MAX_INTEGER) > 0 and
        type(value['policy_sha256']) is str and ledger.HASH.fullmatch(value['policy_sha256']),
        'invalid usage snapshot')
    decimal(value['policy_revision'], maximum=access_policy.MAX_REVISION)
    clients = value['clients']
    ledger.require(type(clients) is list and 1 <= len(clients) <= 64, 'usage client bound')
    ids = set()
    for client in clients:
        ledger.keys(client, ('id', 'window_started', 'window_ns', *COUNTS))
        ledger.require(type(client['id']) is str and
            re.fullmatch('[a-z][a-z0-9_-]{0,31}', client['id']) and client['id'] not in ids and
            type(client['window_started']) is bool, 'invalid usage client')
        ids.add(client['id'])
        window = decimal(client['window_ns'], maximum=ledger.MAX_INTEGER)
        counts = {k: decimal(client[k]) for k in COUNTS}
        ledger.require(window <= int(value['captured_ns']) and
            counts['requests'] <= counts['admitted'] and counts['tokens'] <= counts['settled_tokens'] and
            (client['window_started'] or not (window or counts['requests'] or counts['tokens'])),
            'inconsistent usage snapshot')
    ledger.require(len(ledger.canonical(value)) <= MAX_SNAPSHOT_BYTES, 'usage snapshot byte bound')
    return value


def initial():
    return dict(version=1, revision=0, phase='empty', generation=0, owner=None,
                started_ns='0', source=None, snapshot=None)


def validate(value):
    ledger.keys(value, ('version', 'revision', 'phase', 'generation', 'owner',
                        'started_ns', 'source', 'snapshot'))
    ledger.require(type(value['version']) is int and value['version'] == 1 and
        revision(value['revision']) and ledger.integer(value['generation']) and
        value['phase'] in ('empty', 'active', 'sealed'), 'invalid usage journal')
    started = decimal(value['started_ns'], maximum=ledger.MAX_INTEGER)
    if value['phase'] == 'empty':
        ledger.require(value == initial(), 'invalid initial usage state')
        return value
    ledger.owner_valid(value['owner'])
    ledger.require(value['generation'] > 0 and value['revision'] > 0 and started > 0,
                   'invalid active usage identity')
    source = value['source']
    if source is not None:
        ledger.keys(source, ('generation', 'boot_id'))
        ledger.require(ledger.integer(source['generation'], minimum=1) and
            source['generation'] + 1 == value['generation'] and
            ledger.identifier(source['boot_id']), 'invalid usage predecessor')
    if value['snapshot'] is not None:snapshot(value['snapshot'])
    if value['phase'] == 'active':
        ledger.require((source is None) == (value['snapshot'] is None),
                       'usage recovery source missing')
        if source and source['boot_id'] == value['owner']['boot_id']:
            ledger.require(int(value['snapshot']['captured_ns']) <= started,
                           'usage predecessor is in the future')
    else:
        ledger.require(value['snapshot'] is not None and
            int(value['snapshot']['captured_ns']) >= started, 'usage seal predates activation')
    ledger.require(len(ledger.canonical(value)) <= 64*1024, 'usage state byte bound')
    return value


def digest(value):return ledger.digest(validate(value))


def describe(value):
    validate(value)
    return dict(revision=value['revision'], generation=value['generation'], phase=value['phase'],
                sha256=digest(value), sealed=value['phase'] == 'sealed', model_actions=False)


def begin(previous, generation, owner, now_ns):
    validate(previous);ledger.owner_valid(owner)
    ledger.require(ledger.integer(generation, minimum=1) and generation > previous['generation'] and
        ledger.integer(now_ns, minimum=1), 'usage generation must advance')
    if previous['owner'] is not None and previous['owner']['boot_id'] == owner['boot_id']:
        ledger.require(now_ns >= int(previous['started_ns']), 'usage clock regressed')
    sealed = previous['phase'] == 'sealed' and previous['generation'] + 1 == generation
    result = dict(version=1, revision=previous['revision']+1, phase='active',
        generation=generation, owner=copy.deepcopy(owner), started_ns=str(now_ns),
        source=dict(generation=previous['generation'], boot_id=previous['owner']['boot_id']) if sealed else None,
        snapshot=copy.deepcopy(previous['snapshot']) if sealed else None)
    return validate(result)


def seal(previous, generation, owner, value):
    validate(previous);snapshot(value)
    ledger.require(previous['phase'] == 'active' and previous['generation'] == generation and
                   previous['owner'] == owner, 'usage seal generation changed')
    result = dict(copy.deepcopy(previous), revision=previous['revision']+1,
                  phase='sealed', snapshot=copy.deepcopy(value))
    return validate(result)


def transition(previous, candidate):
    validate(previous);validate(candidate)
    ledger.require(candidate['revision'] == previous['revision']+1, 'usage revision conflict')
    expected = (begin(previous, candidate['generation'], candidate['owner'], int(candidate['started_ns']))
                if candidate['phase'] == 'active' else
                seal(previous, candidate['generation'], candidate['owner'], candidate['snapshot']))
    ledger.require(candidate == expected, 'usage transition changed')
    return candidate


def restoration(value, boot_id, now_ns):
    """Candidate only. The caller must first verify this active state is durable.

    Unknown/cross-boot paths still wait for Guardian's full conservative gate.
    Cross-boot sealed totals can survive; the old monotonic window cannot.
    """
    validate(value)
    ledger.require(value['phase'] == 'active' and value['owner']['boot_id'] == boot_id and
        ledger.integer(now_ns, minimum=1) and now_ns >= int(value['started_ns']),
        'invalid usage restoration context')
    if value['snapshot'] is None:return dict(mode='unknown', snapshot=None)
    result = copy.deepcopy(value['snapshot'])
    if value['source']['boot_id'] == boot_id:
        return dict(mode='exact', snapshot=result)
    result['captured_ns'] = str(now_ns)
    for client in result['clients']:
        client.update(window_started=False, window_ns='0', requests='0', tokens='0')
    snapshot(result)
    return dict(mode='cross_boot', snapshot=result)
