"""Pure exact-cycle restoration for a stopped root deployment transaction.

The coordinator must authenticate the registration and latest checkpoint under
the real service locks. A JSON snapshot or this policy alone grants no file,
device, model, or role authority. In particular an initialization seed is not
evidence that an inactive root's fallback remains unused.
"""
import copy

from tools.serve_recovery import deployment_policy, ledger, version_policy

FORMAT = 'q4t-root-version-transfer-v1'
MAX_BYTES = 64*1024


def validate(value):
    ledger.keys(value, ('format', 'deployment', 'before', 'checkpoint',
                        'target_registry', 'checkpoint_sha256', 'candidate'))
    ledger.require(value['format'] == FORMAT, 'invalid root version transfer')
    deployment = deployment_policy.validate(value['deployment'])
    ledger.require(deployment['phase'] == 'prepared',
                   'root version transfer needs a prepared deployment')
    before = version_policy.validate(value['before'])
    saved = version_policy.validate(value['checkpoint'])
    target = version_policy.registry(value['target_registry'])
    following = version_policy.validate(value['candidate'])
    ledger.require(before['registry']['root_manifest_sha256'] ==
        deployment['current']['manifest_sha256'] and
        target['root_manifest_sha256'] == deployment['target']['manifest_sha256'] and
        deployment['current']['manifest_sha256'] != deployment['target']['manifest_sha256'],
        'version transfer source or target root differs')
    ledger.require(saved['registry'] == target and
        saved['registry']['cycle_id'] != before['registry']['cycle_id'],
        'target checkpoint registration differs')
    ledger.require(saved['revision'] <= before['revision'] and
        value['checkpoint_sha256'] == version_policy.digest(saved),
        'checkpoint is ahead of current state or its identity changed')
    expected = dict(copy.deepcopy(saved), revision=before['revision']+1)
    ledger.require(following == expected,
                   'root transfer changed saved recovery state or refunded a cycle')
    ledger.require(len(ledger.canonical(value)) <= MAX_BYTES,
                   'root version transfer byte bound')
    return value


def prepare(deployment, before, checkpoint, target_registry):
    """No clocks, new cycle UUIDs, initial seeds or model actions are created."""
    return validate(dict(format=FORMAT, deployment=copy.deepcopy(deployment),
        before=copy.deepcopy(before), checkpoint=copy.deepcopy(checkpoint),
        target_registry=copy.deepcopy(target_registry),
        checkpoint_sha256=version_policy.digest(checkpoint),
        candidate=dict(copy.deepcopy(checkpoint), revision=before['revision']+1)))


def authorized(value, before, following):
    """Narrow journal transition; actual ownership is a coordinator duty."""
    validate(value)
    ledger.require(before == value['before'] and following == value['candidate'],
                   'version transition is not the exact reviewed root transfer')
    return following


def request_id(value):
    validate(value)
    import uuid
    return str(uuid.uuid5(uuid.UUID(value['deployment']['request']),
                          'preserve-root-version-cycle'))


def describe(value):
    validate(value)
    return dict(request_id=request_id(value),
        source_cycle=value['before']['registry']['cycle_id'],
        target_cycle=value['candidate']['registry']['cycle_id'],
        checkpoint_sha256=value['checkpoint_sha256'],
        fallback_consumed=value['candidate']['switch'] is not None,
        terminal_fault_preserved=value['candidate']['terminal_fault'] is not None,
        creates_new_cycle=False, automatic_start=False, model_actions=False)
