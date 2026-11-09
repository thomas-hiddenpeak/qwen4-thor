"""Bounded STOPPED deployment selection; no credentials or core budget state."""
import copy
from pathlib import Path

from tools.serve_recovery import access_policy, ledger

MAX_BYTES = 128 * 1024
revision = access_policy.revision


def descriptor(value):
    version = value.get('version') if type(value) is dict else None
    ledger.keys(value, ('version', 'service_id', 'package', 'manifest_sha256',
        *(('deployment_revision', 'deployment_id') if version == 2 else ())))
    ledger.require(type(version) is int and version in (1, 2) and
        ledger.identifier(value['service_id']) and type(value['package']) is str and
        1 <= len(value['package']) <= 4096 and Path(value['package']).is_absolute() and
        type(value['manifest_sha256']) is str and ledger.HASH.fullmatch(value['manifest_sha256']),
        'invalid deployment descriptor')
    if version == 2:
        ledger.require(revision(value['deployment_revision'], minimum=1) and
                       ledger.identifier(value['deployment_id']), 'invalid deployment identity')
    return value


def initial(current):
    return dict(version=1, revision=0, phase='stable', current=copy.deepcopy(descriptor(current)),
                target=None, request=None, seeds=[], history=[])


def validate(value):
    ledger.keys(value, ('version', 'revision', 'phase', 'current', 'target', 'request', 'seeds', 'history'))
    ledger.require(type(value['version']) is int and value['version'] == 1 and
                   revision(value['revision']) and value['phase'] in ('stable', 'prepared'),
                   'invalid deployment journal')
    current = descriptor(value['current'])
    history = value['history']
    ledger.require(type(history) is list and len(history) <= 16, 'deployment history bound')
    for item in history:
        ledger.require(descriptor(item)['service_id'] == current['service_id'], 'deployment service changed')
    seeds = value['seeds']
    ledger.require(type(seeds) is list and seeds == sorted(set(seeds)) and
                   set(seeds) <= {'access', 'limits', 'usage'}, 'deployment bootstrap identity')
    if value['phase'] == 'stable':
        ledger.require(value['target'] is None and value['request'] is None and not seeds,
                       'stable deployment has pending work')
    else:
        target = descriptor(value['target'])
        ledger.require(target['version'] == 2 and target['service_id'] == current['service_id'] and
            target['deployment_revision'] == current.get('deployment_revision', 0)+1 and
            ledger.identifier(value['request']) and target['deployment_id'] == value['request'],
            'invalid prepared deployment')
    ledger.require(len(ledger.canonical(value)) <= 96*1024, 'deployment byte bound')
    return value


def digest(value):return ledger.digest(validate(value))


def describe(value):
    validate(value)
    return dict(revision=value['revision'], phase=value['phase'], current=value['current'],
                target=value['target'], history=value['history'], model_actions=False)


def prepare(previous, target, request_id, seeds):
    validate(previous)
    ledger.require(previous['phase'] == 'stable', 'deployment already pending')
    return validate(dict(copy.deepcopy(previous), revision=previous['revision']+1,
                         phase='prepared', target=copy.deepcopy(target), request=request_id, seeds=seeds))


def finish(previous):
    validate(previous)
    ledger.require(previous['phase'] == 'prepared', 'deployment is not pending')
    return validate(dict(version=1, revision=previous['revision']+1, phase='stable',
        current=copy.deepcopy(previous['target']), target=None, request=None, seeds=[],
        history=[*copy.deepcopy(previous['history'][-15:]), copy.deepcopy(previous['current'])]))


def transition(previous, candidate):
    validate(previous);validate(candidate)
    expected = (prepare(previous, candidate['target'], candidate['request'], candidate['seeds'])
                if previous['phase'] == 'stable' else finish(previous))
    ledger.require(candidate == expected, 'deployment transition changed')
    return candidate
