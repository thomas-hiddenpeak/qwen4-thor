"""Durable default-publication intent; deployment history is never client state."""
import copy
from pathlib import Path

from tools.serve_recovery import access_policy, ledger

MAX_BYTES = 128 * 1024
MAX_HISTORY = 16
revision = access_policy.revision
SNAPSHOT = ('generation_sha256', 'package', 'manifest_sha256',
            'engine_binary_sha256', 'engine_release_sha256', 'service_release_sha256',
            'service_acceptance_sha256')


def snapshot(value):
    ledger.keys(value, SNAPSHOT)
    ledger.require(type(value['package']) is str and
        1 <= len(value['package']) <= 4096 and Path(value['package']).is_absolute() and
        all(type(value[k]) is str and ledger.HASH.fullmatch(value[k])
            for k in SNAPSHOT if k != 'package'), 'invalid publication generation')
    return value


def initial(root, current, *, fresh=False):
    return validate(dict(version=1, revision=0, root=str(root), phase='unpublished',
        current=copy.deepcopy(snapshot(current)), target=None, request_id=None,
        rollback=False, fresh=fresh, history=[]))


def validate(value):
    ledger.keys(value, ('version', 'revision', 'root', 'phase', 'current', 'target',
                        'request_id', 'rollback', 'fresh', 'history'))
    ledger.require(type(value['version']) is int and value['version'] == 1 and
        revision(value['revision']) and type(value['root']) is str and
        1 <= len(value['root']) <= 4096 and Path(value['root']).is_absolute() and
        value['phase'] in ('unpublished', 'prepared', 'stable') and
        type(value['rollback']) is bool and type(value['fresh']) is bool,
        'invalid publication state')
    snapshot(value['current'])
    ledger.require(type(value['history']) is list and len(value['history']) <= MAX_HISTORY,
                   'publication history bound')
    requests = set()
    for item in value['history']:
        ledger.keys(item, ('request_id', 'previous', 'selected', 'rollback'))
        ledger.require(ledger.identifier(item['request_id']) and
            item['request_id'] not in requests and type(item['rollback']) is bool,
            'invalid publication receipt')
        snapshot(item['previous']);snapshot(item['selected'])
        requests.add(item['request_id'])
    ledger.require(all(a['selected'] == b['previous'] for a,b in
        zip(value['history'],value['history'][1:])), 'publication history chain changed')
    ledger.require(not value['history'] or value['history'][-1]['selected'] == value['current'],
                   'publication current differs from history')
    if value['phase'] == 'prepared':
        snapshot(value['target'])
        ledger.require(ledger.identifier(value['request_id']) and
                       value['request_id'] not in requests, 'invalid publication request')
    else:
        ledger.require(value['target'] is None and value['request_id'] is None and
                       value['rollback'] is False, 'publication has unresolved intent')
    if value['phase'] == 'unpublished':
        ledger.require(value['revision'] == 0 and not value['history'],
                       'unpublished generation has prior history')
    elif value['phase'] == 'stable':
        ledger.require(not value['fresh'] and bool(value['history']) and
            value['history'][-1]['selected'] == value['current'], 'publication lacks receipt')
    ledger.require(len(ledger.canonical(value)) <= 96 * 1024, 'publication byte bound')
    return value


def digest(value):return ledger.digest(validate(value))


def prepare(previous, target, request_id, rollback):
    validate(previous);snapshot(target)
    ledger.require(previous['phase'] in ('unpublished', 'stable'), 'publication already pending')
    ledger.require(type(rollback) is bool, 'invalid publication rollback flag')
    if rollback:
        ledger.require(any(target in (h['previous'], h['selected']) for h in previous['history']),
                       'publication rollback target is not retained history')
    return validate(dict(copy.deepcopy(previous), revision=previous['revision']+1,
        phase='prepared', target=copy.deepcopy(target), request_id=request_id, rollback=rollback))


def finish(previous):
    validate(previous)
    ledger.require(previous['phase'] == 'prepared', 'publication is not pending')
    receipt = dict(request_id=previous['request_id'], previous=copy.deepcopy(previous['current']),
                   selected=copy.deepcopy(previous['target']), rollback=previous['rollback'])
    return validate(dict(version=1, revision=previous['revision']+1, root=previous['root'],
        phase='stable', current=copy.deepcopy(previous['target']), target=None,
        request_id=None, rollback=False, fresh=False,
        history=[*copy.deepcopy(previous['history'][-15:]), receipt]))


def transition(previous, candidate):
    validate(previous);validate(candidate)
    expected = (finish(previous) if previous['phase'] == 'prepared' else
        prepare(previous, candidate['target'], candidate['request_id'], candidate['rollback']))
    ledger.require(candidate == expected, 'publication transition changed')
    return candidate


def describe(value):
    validate(value)
    return dict(revision=value['revision'], phase=value['phase'], root=value['root'],
        current=value['current'], target=value['target'], request_id=value['request_id'],
        history=value['history'], model_actions=False, automatic_start=False)
