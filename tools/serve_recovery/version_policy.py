"""One durable older-payload recovery per explicitly registered repair cycle.

Only the native recovery root may supply independently checked fault facts.
One confirmed code failure consumes the fallback; ordinary unknown exits keep
the existing restart path. Waiting for a second confirmed failure would reach
the core ledger's repeated-fault quarantine and must not be used to bypass it.
This policy does not inspect hardware, trust controller health claims, grant
role launches, or signal any process. Original Guardian budgets still apply.
"""
import copy

from tools.serve_recovery import ledger

FORMAT = 'q4t-version-selection-v1'
REGISTRY_FORMAT = 'q4t-version-registry-v1'
MAX_BYTES = 128*1024
ROLES = ('controller', 'model', 'gateway')
PAYLOADS = ('primary', 'fallback')
FAULT_KINDS = ('native_process_signal', 'execution_contract_failure')
GUARDS = ('independent_observation', 'identity_valid', 'lease_continuous',
          'resources_safe', 'device_usable')


def revision(value, *, minimum=0):
    return ledger.integer(value, minimum=minimum)


def hash_value(value):
    ledger.require(type(value) is str and ledger.HASH.fullmatch(value),
                   'invalid version identity')


def generations(value, *, minimum=0):
    ledger.keys(value, ROLES)
    ledger.require(all(ledger.integer(v, minimum=minimum) for v in value.values()),
                   'invalid version generations')
    return value


def registry(value):
    ledger.keys(value, ('format', 'cycle_id', 'root_manifest_sha256', 'payloads',
                        'compatibility_sha256', 'qualification_sha256'))
    ledger.require(value['format'] == REGISTRY_FORMAT and
                   ledger.identifier(value['cycle_id']), 'invalid version registry')
    for key in ('root_manifest_sha256', 'compatibility_sha256', 'qualification_sha256'):
        hash_value(value[key])
    ledger.keys(value['payloads'], PAYLOADS)
    for payload in value['payloads'].values():
        ledger.keys(payload, ('manifest_sha256', 'binding_sha256'))
        for h in payload.values(): hash_value(h)
    ledger.require(value['payloads']['primary']['manifest_sha256'] !=
        value['payloads']['fallback']['manifest_sha256'], 'fallback is not another payload')
    return value


def initial(registration, boot_id):
    registry(registration)
    ledger.require(ledger.identifier(boot_id), 'invalid version boot identity')
    return validate(dict(format=FORMAT, revision=0, registry=copy.deepcopy(registration),
        boot_id=boot_id, selected='primary', faults=[], switch=None,
        completed=None, terminal_fault=None))


def fault(value):
    ledger.keys(value, ('role', 'generation', 'boot_id', 'payload_sha256',
                        'kind', 'fingerprint', 'signal'))
    ledger.require(value['role'] in ROLES and
        ledger.integer(value['generation'], minimum=1) and ledger.identifier(value['boot_id']) and
        type(value['kind']) is str and 1 <= len(value['kind']) <= 64 and
        ledger.integer(value['signal']) and value['signal'] <= 64, 'invalid version fault')
    hash_value(value['payload_sha256']); hash_value(value['fingerprint'])
    return value


def recognized(event):
    return ((event['kind'] == 'native_process_signal' and event['signal'] in (4, 6, 11)) or
            (event['kind'] == 'execution_contract_failure' and event['signal'] == 0))


def validate(value):
    ledger.keys(value, ('format', 'revision', 'registry', 'boot_id', 'selected',
                        'faults', 'switch', 'completed', 'terminal_fault'))
    ledger.require(value['format'] == FORMAT and revision(value['revision']) and
        ledger.identifier(value['boot_id']) and value['selected'] in PAYLOADS,
        'invalid version selection')
    payloads = registry(value['registry'])['payloads']
    events = value['faults']
    ledger.require(type(events) is list and len(events) <= 1, 'unbounded version faults')
    seen = set()
    for event in events:
        fault(event)
        key = (event['role'], event['generation'])
        ledger.require(recognized(event) and key not in seen and
            event['payload_sha256'] == payloads['primary']['manifest_sha256'],
            'invalid retained primary version fault')
        seen.add(key)
    intent = value['switch']
    if intent is None:
        ledger.require(value['selected'] == 'primary' and value['completed'] is None and
                       value['terminal_fault'] is None, 'version selection lacks consumed intent')
    else:
        ledger.keys(intent, ('request_id', 'fault', 'generations'))
        ledger.require(ledger.identifier(intent['request_id']) and len(events) == 1 and
            intent['fault'] == events[-1] and
            intent['generations'][events[-1]['role']] == events[-1]['generation'],
            'version switch lacks an independently verified source generation')
        generations(intent['generations'])
    completed = value['completed']
    if completed is not None:
        ledger.keys(completed, ('generations', 'binding_sha256'))
        generations(completed['generations'], minimum=1)
        ledger.require(value['selected'] == 'fallback' and intent is not None and
            all(completed['generations'][r] > intent['generations'][r] for r in ROLES) and
            completed['binding_sha256'] == payloads['fallback']['binding_sha256'],
            'version completion did not independently bind new payload tree')
    else:
        ledger.require(value['selected'] == 'primary', 'fallback selection is unconfirmed')
    terminal = value['terminal_fault']
    if terminal is not None:
        fault(terminal)
        ledger.require(intent is not None and recognized(terminal) and
            terminal['payload_sha256'] == payloads['fallback']['manifest_sha256'] and
            terminal['generation'] > intent['generations'][terminal['role']],
            'invalid terminal fallback fault')
    ledger.require(len(ledger.canonical(value)) <= 16*1024, 'version state byte bound')
    return value


def context(value):
    ledger.keys(value, ('desired', 'boot_id', 'payload_sha256', 'generations',
                        'termination_requested', *GUARDS))
    ledger.require(value['desired'] in ('RUNNING', 'STOPPED', 'MAINTENANCE') and
        ledger.identifier(value['boot_id']) and all(type(value[k]) is bool
        for k in (*GUARDS, 'termination_requested')), 'invalid version proof context')
    generations(value['generations']); hash_value(value['payload_sha256'])
    return value


def launch_payload(value):
    validate(value)
    return 'fallback' if value['switch'] is not None else 'primary'


def assess(previous, event, proof):
    validate(previous); fault(event); context(proof)
    reason = None
    selected = launch_payload(previous)
    if proof['desired'] != 'RUNNING': reason = 'operator_intent'
    elif proof['termination_requested']: reason = 'owned_termination'
    elif previous['terminal_fault'] is not None: reason = 'version_recovery_exhausted'
    elif (event['boot_id'] != proof['boot_id'] or proof['boot_id'] != previous['boot_id'] or
          event['payload_sha256'] != proof['payload_sha256'] or
          event['payload_sha256'] != previous['registry']['payloads'][selected]['manifest_sha256'] or
          event['generation'] != proof['generations'][event['role']]): reason = 'stale_version_fault'
    elif not recognized(event): reason = 'not_version_fault'
    elif not all(proof[k] for k in GUARDS): reason = 'independent_safety_proof_missing'
    elif any(e['role'] == event['role'] and e['generation'] == event['generation']
             for e in previous['faults']): reason = 'generation_already_recorded'
    elif selected == 'fallback' and event['generation'] <= previous['switch']['generations'][event['role']]:
        reason = 'fallback_generation_not_new'
    return dict(accepted=reason is None, reason=reason or 'verified_version_fault')


def observe(previous, event, proof, request_id):
    checked = assess(previous, event, proof)
    ledger.require(checked['accepted'], checked['reason'])
    ledger.require(ledger.identifier(request_id), 'invalid version switch request')
    value = copy.deepcopy(previous); value['revision'] += 1
    if value['switch'] is not None:
        value['terminal_fault'] = copy.deepcopy(event)
    else:
        value['faults'] = [copy.deepcopy(event)]
        value['switch'] = dict(request_id=request_id, fault=copy.deepcopy(event),
                               generations=copy.deepcopy(proof['generations']))
    return validate(value)


def resume(previous, boot_id):
    value = copy.deepcopy(validate(previous))
    ledger.require(ledger.identifier(boot_id), 'invalid resumed version boot')
    value.update(revision=value['revision']+1, boot_id=boot_id)
    return validate(value)


def finish(previous, proof, binding_sha256):
    validate(previous); context(proof)
    ledger.require(previous['switch'] is not None and previous['completed'] is None and
        previous['terminal_fault'] is None and proof['desired'] == 'RUNNING' and
        not proof['termination_requested'] and all(proof[k] for k in GUARDS) and
        proof['boot_id'] == previous['boot_id'] and proof['payload_sha256'] ==
        previous['registry']['payloads']['fallback']['manifest_sha256'],
        'version completion lacks independent active proof')
    value = copy.deepcopy(previous)
    value.update(revision=value['revision']+1, selected='fallback', completed=dict(
        generations=copy.deepcopy(proof['generations']), binding_sha256=binding_sha256))
    return validate(value)


def transition(previous, candidate):
    """Validate protected history; actual fault authority lives in native root.

    The integration must restrict its I/O channel to the native root. A valid
    receipt cannot authorize any role launch or overwrite the core ledger.
    """
    validate(previous); validate(candidate)
    ledger.require(candidate['revision'] == previous['revision']+1 and
        candidate['registry'] == previous['registry'], 'version cycle changed at runtime')
    if candidate == resume(previous, candidate['boot_id']): return candidate
    ledger.require(candidate['boot_id'] == previous['boot_id'], 'fault boot changed')
    # Reconstruct all protected data. The root independently validates these
    # live safety facts before submitting, rather than trusting journal content.
    proof = dict(desired='RUNNING', boot_id=previous['boot_id'], termination_requested=False,
                 **{k:True for k in GUARDS})
    if candidate['completed'] != previous['completed']:
        proof.update(payload_sha256=previous['registry']['payloads']['fallback']['manifest_sha256'],
                     generations=candidate['completed']['generations'])
        expected = finish(previous, proof, candidate['completed']['binding_sha256'])
    else:
        event = candidate['terminal_fault'] if candidate['terminal_fault'] != previous['terminal_fault'] \
            else candidate['faults'][-1]
        if previous['switch'] is None and candidate['switch'] is not None:
            observed_generations = candidate['switch']['generations']
            request = candidate['switch']['request_id']
        else:
            observed_generations = {r:0 for r in ROLES}
            observed_generations[event['role']] = event['generation']
            request = event['boot_id']  # No new switch is created on this path.
        proof.update(payload_sha256=event['payload_sha256'], generations=observed_generations)
        expected = observe(previous, event, proof, request)
    ledger.require(candidate == expected, 'version transition changed protected history')
    return candidate


def digest(value): return ledger.digest(validate(value))


def describe(value):
    validate(value)
    return dict(revision=value['revision'], cycle_id=value['registry']['cycle_id'],
        selected=value['selected'], launch_payload=launch_payload(value),
        pending=value['switch'] is not None and value['completed'] is None,
        fallback_consumed=value['switch'] is not None,
        protected=value['terminal_fault'] is not None,
        model_actions=False, role_launch_authority=False)
