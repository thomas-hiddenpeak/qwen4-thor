"""Qualified version/capacity migration without changing historical budgets.

Only a STOPPED deployment owner or the independent native version root after
whole-tree retirement may construct the authorized store contract, using both
sealed packages and its durable deployment/version intent. Ordinary
runtime profile workers retain profile_policy and cannot authorize migration.
Explicit capacity changes retain historical qualifications in state v2. Auto
fallback remains capacity-preserving and never changes the user's contract.
"""
import copy

from tools.serve_recovery import ledger, profile_contract, profile_evidence
from tools.serve_recovery import profile_policy as policy

FORMAT = 'q4t-profile-migration-permit-v1'
CAPACITY_FORMAT = 'q4t-profile-migration-permit-v2'
EXTENSION_FORMAT = 'q4t-profile-migration-permit-v3'
RESPONSE_OUTPUT_TYPES = frozenset(('message', 'reasoning', 'function_call',
    'custom_tool_call', 'compaction', 'file_search_call', 'tool_search_call',
    'tool_search_output', 'program', 'program_output'))


def semantic_capabilities(value):
    """Separate reviewed implementation identities from promised behavior.

    The full original observations remain in the sealed registry/evidence.
    v4 adds model refusal framing to the unchanged v3 message input schema;
    refusal itself remains a capability and cannot disappear on recovery.
    Renderer and tool-history fingerprints identify code, not capabilities;
    the original hashes stay bound in each sealed qualification. Unknown
    template revisions and other changed values are not normalized.
    """
    result = copy.deepcopy(value)
    responses = result.get('responses')
    if type(responses) is dict and 'output_items' in responses:
        items = responses['output_items']
        ledger.require(type(items) is list and all(type(x) is str for x in items)
            and len(set(items)) == len(items) and
            set(items) <= RESPONSE_OUTPUT_TYPES, 'invalid response output capabilities')
        if 'file_search_call' in items:
            search = responses.get('file_search')
            ledger.require(type(search) is dict and
                search.get('profile') == 'q4t-file-search-v1' and
                search.get('implemented') is True,
                'file search output lacks its implementation capability')
        discovery_items = {'tool_search_call', 'tool_search_output'}
        if discovery_items.intersection(items):
            discovery = responses.get('tool_discovery')
            ledger.require(discovery_items.issubset(items) and
                type(discovery) is dict and
                discovery.get('profile') == 'q4t-tool-discovery-v1' and
                discovery.get('definitions') == 'ordered_history' and
                all(discovery.get(key) is True for key in ('defer_loading',
                    'server_paths', 'client_search', 'additional_tools')),
                'tool discovery outputs lack their implementation capability')
        program_items = {'program', 'program_output'}
        if program_items.intersection(items):
            program = responses.get('programmatic_tools')
            ledger.require(program_items.issubset(items) and
                type(program) is dict and
                program.get('profile') == 'q4t-programmatic-tools-v1' and
                program.get('caller_binding') == 'program_call_id' and
                program.get('schema_binding') == 'invocation_time' and
                all(program.get(key) is True for key in ('enabled',
                    'allowed_callers', 'authenticated_continuation',
                    'isolated_worker', 'loaded_tools_only')) and
                program.get('automatic_tool_replay') is False and
                program.get('model_restart_on_error') is False,
                'program outputs lack their implementation capability')
        # This field declares an unordered set of output types. Normalize only
        # this reviewed enum; all other arrays retain exact-value comparison.
        responses['output_items'] = {item: True for item in items}
    template = result.get('template')
    if isinstance(template, dict):
        for key in ('renderer_source_sha256', 'tool_history_source_sha256'):
            if key in template:
                fingerprint = template.pop(key)
                ledger.require(type(fingerprint) is str and
                    ledger.HASH.fullmatch(fingerprint),
                    'invalid template source identity: '+key)
        if template.get('message_profile') == 'qwen3.8-openai-v4':
            refusal = result.get('chat', {}).get('refusal_output', {})
            ledger.require(type(refusal) is dict and
                refusal.get('profile') == 'q4t-refusal-v1',
                'v4 template lacks its refusal capability')
            template['message_profile'] = 'qwen3.8-openai-v3'
    return result


def extends(previous, target):
    """Object additions only; deletion, replacement and type coercion fail."""
    if type(previous) is not type(target):
        return False
    if type(previous) is dict:
        return all(key in target and extends(value, target[key])
                   for key, value in previous.items())
    return previous == target


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
           previous_config, target_config, *, automatic=False,
           allow_capacity_change=False, allow_capability_extension=False):
    first = observations(previous, previous_evidence)
    second = observations(target, target_evidence)
    old_contract, new_contract = map(semantic_capabilities, (first, second))
    extension = old_contract != new_contract
    ledger.require(type(allow_capability_extension) is bool and
        (not extension or (allow_capability_extension and not automatic and
                           extends(old_contract, new_contract))),
        'migration would change application capabilities without an explicit '
        'qualified extension; automatic recovery cannot lose or add features')
    changed = previous['profiles'] != target['profiles']
    ledger.require(type(allow_capacity_change) is bool and
        (not changed or (allow_capacity_change and not automatic)),
        'capacity change requires explicit historical-qualification migration')
    for key in ('model_files', 'model_assets', 'native_embedding'):
        ledger.require(previous['identity'][key] == target['identity'][key],
                       'migration would change fixed model identity')
    policy.configuration(previous_config); policy.configuration(target_config)
    ledger.require(type(automatic) is bool and
        (not automatic or previous_config == target_config),
        'automatic fallback cannot alter profile policy')
    return validate(dict(format=EXTENSION_FORMAT if extension else
        CAPACITY_FORMAT if changed else FORMAT,
        from_registry=profile_contract.digest(previous),
        to_registry=profile_contract.digest(target),
        profiles={n:ledger.digest(v) for n,v in previous['profiles'].items()},
        from_configuration=copy.deepcopy(previous_config),
        to_configuration=copy.deepcopy(target_config),
        from_identity=ledger.digest(previous['identity']),
        to_identity=ledger.digest(target['identity']),
        capabilities_sha256=ledger.digest(first), automatic=automatic,
        **(dict(target_profiles={n:ledger.digest(v) for n,v in target['profiles'].items()})
           if changed or extension else {}),
        **(dict(target_capabilities_sha256=ledger.digest(second)) if extension else {})))


def validate(value):
    capacity_change = type(value) is dict and value.get('format') == CAPACITY_FORMAT
    extension = type(value) is dict and value.get('format') == EXTENSION_FORMAT
    ledger.keys(value, ('format','from_registry','to_registry','profiles',
        'from_configuration','to_configuration','from_identity','to_identity',
        'capabilities_sha256','automatic',
        *(('target_profiles',) if capacity_change or extension else ()),
        *(('target_capabilities_sha256',) if extension else ())))
    ledger.require(value['format'] in (FORMAT, CAPACITY_FORMAT, EXTENSION_FORMAT) and
                   type(value['automatic']) is bool,
                   'invalid profile migration permit')
    if capacity_change or extension:
        ledger.keys(value['target_profiles'], profile_contract.NAMES)
        ledger.require(not value['automatic'] and
            (extension or value['target_profiles'] != value['profiles']),
            'capacity/feature migration must be explicit')
    if extension:
        ledger.require(value['target_capabilities_sha256'] != value['capabilities_sha256'],
                       'extension must bind different actual capabilities')
    ledger.keys(value['profiles'], profile_contract.NAMES)
    for h in [value[k] for k in ('from_registry','to_registry','from_identity',
                               'to_identity','capabilities_sha256')]+list(value['profiles'].values())+list(
                                   value.get('target_profiles', {}).values())+(
                                   [value['target_capabilities_sha256']] if extension else []):
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
    if authorization['format'] in (CAPACITY_FORMAT, EXTENSION_FORMAT) and \
            authorization['target_profiles'] != previous['profiles']:
        value = policy.historical(value)
        value['profiles'] = copy.deepcopy(authorization['target_profiles'])
    value.update(registry_sha256=authorization['to_registry'],
                 configuration=copy.deepcopy(authorization['to_configuration']))
    if value['history']:
        value['not_before_ns'] = max(value['not_before_ns'],
            value['history'][-1]['at_logical_ns']+
            value['configuration']['cooldown_seconds']*ledger.SECOND)
    if value['light_since_ns'] is not None:
        # Residency was also an established wait, even though v1 represented
        # it as light_since + configuration rather than a stored deadline.
        value['not_before_ns'] = max(value['not_before_ns'],
            value['light_since_ns']+max(
                previous['configuration']['minimum_light_seconds'],
                value['configuration']['minimum_light_seconds'])*ledger.SECOND)
    return policy.validate(value)


class AuthorizedContract:
    """Authority supplied by locked deployment or the private native root.

    On a crash the durable deployment intent must be reverified to reconstruct
    this contract. No migration token is copied into an ordinary profile worker request
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
