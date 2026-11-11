"""Qualified same-capacity migration without changing historical budgets.

Only a STOPPED deployment owner may construct the authorized store contract
after verifying both sealed packages and a durable deployment intent. Ordinary
runtime profile workers retain profile_policy and cannot authorize migration.
Capacity-history format evolution and automatic payload selection are separate
R06 substeps, not implemented by this pure representation contract.
"""
import copy

from tools.serve_recovery import ledger, profile_contract, profile_evidence
from tools.serve_recovery import profile_policy as policy

FORMAT = 'q4t-profile-migration-permit-v1'


def observations(registry, evidence):
    profile_contract.validate(registry)
    ledger.keys(evidence, profile_contract.NAMES)
    for name, item in evidence.items():
        ledger.keys(item, ('record', 'sha256'))
        profile_evidence.validate(item['record'], registry, name, item['sha256'])
    result = profile_contract.capabilities(evidence['primary']['record']['capabilities'], True)
    # The protocol fingerprint identifies code, not a feature that may be
    # silently lost on recovery. Each original fingerprint remains bound to
    # its own actual engine/qualification and independent admission checks.
    del result['protocol_source_sha256']
    return result


def permit(previous, target, previous_evidence, target_evidence,
           previous_config, target_config, *, automatic=False):
    first = observations(previous, previous_evidence)
    second = observations(target, target_evidence)
    ledger.require(first == second, 'migration would change application capabilities')
    ledger.require(previous['profiles'] == target['profiles'],
                   'capacity change requires historical-qualification format migration')
    for key in ('model_files', 'model_assets', 'native_embedding'):
        ledger.require(previous['identity'][key] == target['identity'][key],
                       'migration would change fixed model identity')
    policy.configuration(previous_config); policy.configuration(target_config)
    ledger.require(type(automatic) is bool and
        (not automatic or previous_config == target_config),
        'automatic fallback cannot alter profile policy')
    return validate(dict(format=FORMAT,
        from_registry=profile_contract.digest(previous),
        to_registry=profile_contract.digest(target),
        profiles={n:ledger.digest(v) for n,v in previous['profiles'].items()},
        from_configuration=copy.deepcopy(previous_config),
        to_configuration=copy.deepcopy(target_config),
        from_identity=ledger.digest(previous['identity']),
        to_identity=ledger.digest(target['identity']),
        capabilities_sha256=ledger.digest(first), automatic=automatic))


def validate(value):
    ledger.keys(value, ('format','from_registry','to_registry','profiles',
        'from_configuration','to_configuration','from_identity','to_identity',
        'capabilities_sha256','automatic'))
    ledger.require(value['format'] == FORMAT and type(value['automatic']) is bool,
                   'invalid profile migration permit')
    ledger.keys(value['profiles'], profile_contract.NAMES)
    for h in [value[k] for k in ('from_registry','to_registry','from_identity',
                               'to_identity','capabilities_sha256')]+list(value['profiles'].values()):
        ledger.require(type(h) is str and ledger.HASH.fullmatch(h),
                       'invalid profile migration identity')
    policy.configuration(value['from_configuration'])
    policy.configuration(value['to_configuration'])
    ledger.require(not value['automatic'] or
        value['from_configuration'] == value['to_configuration'],
        'automatic profile policy change')
    return value


def candidate(previous, authorization, stamp):
    validate(authorization); policy.validate(previous)
    ledger.require(previous['registry_sha256'] == authorization['from_registry'] and
        previous['profiles'] == authorization['profiles'] and
        previous['configuration'] == authorization['from_configuration'],
        'profile migration predecessor differs')
    # No elapsed time, including same-boot STOPPED time, earns new budget.
    value = policy.resume(previous, stamp)
    value.update(registry_sha256=authorization['to_registry'],
                 configuration=copy.deepcopy(authorization['to_configuration']))
    if value['history']:
        value['not_before_ns'] = max(value['not_before_ns'],
            value['history'][-1]['at_logical_ns']+
            value['configuration']['cooldown_seconds']*ledger.SECOND)
    return policy.validate(value)


class AuthorizedContract:
    """In-memory authority supplied by locked deployment, never runtime IPC.

    On a crash the durable deployment intent must be reverified to reconstruct
    this contract. No migration token is copied into an ordinary worker request
    or the backward-readable profile state. Original receipts remain intact.
    """
    MAX_BYTES = policy.MAX_BYTES
    validate = staticmethod(policy.validate)
    revision = staticmethod(policy.revision)
    digest = staticmethod(policy.digest)

    def __init__(self, authorization):
        self.authorization = copy.deepcopy(validate(authorization))

    def transition(self, previous, target):
        try:
            return policy.transition(previous, target)
        except (ValueError, KeyError, TypeError):
            stamp = ledger.Stamp(target['clock']['boot_id'], target['clock']['monotonic_ns'])
            expected = candidate(previous, self.authorization, stamp)
            ledger.require(target == expected, 'migration changed protected profile history')
            return target
