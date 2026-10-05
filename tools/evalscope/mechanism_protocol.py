"""Frozen cache/route observation pairs; never performance acceptance."""
import hashlib
import json
from pathlib import Path
import re

from offload_policy import DIAGNOSTIC_ENVIRONMENT, policy_environment

MECHANISM_SCOPE = 'offload_cache_mechanism_v1'
GROUP_ORDER = (('AS', 'off'), ('AS', 'on'), ('CS', 'on'), ('CS', 'off'),
               ('CL', 'off'), ('CL', 'on'), ('AL', 'on'), ('AL', 'off'))
GROUP_FIELDS = {'id', 'index', 'pair_index', 'pair_position', 'condition',
                'arm', 'predecessor', 'predecessor_tokens', 'observation',
                'requests'}
REQUEST_FIELDS = {'position', 'role', 'input_tokens', 'output_tokens'}
RESULT_FIELDS = ('group_id', 'group_index', 'pair_index', 'pair_position',
                 'condition', 'arm', 'predecessor', 'predecessor_tokens',
                 'observation', 'sequence_sha256', 'phase_plan_sha256')
REQUEST_RESULT_FIELDS = ('case', 'position', 'role', 'input_tokens',
                         'preceding_case', 'preceding_input_tokens')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest_value(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'duplicate mechanism JSON key: ' + key)
        value[key] = item
    return value


def mechanism_plan(document, sequence_sha256, max_len=262144):
    """Derive exactly four requests and the observation bundle from position."""
    require(type(document) is dict and set(document) ==
            {'schema', 'scope', 'phase_plan_sha256', 'trace_max_mib', 'group'},
            'invalid mechanism document fields')
    require(type(document['schema']) is int and document['schema'] == 1 and
            document['scope'] == MECHANISM_SCOPE,
            'unsupported mechanism schema or scope')
    require(digest_value(sequence_sha256) and
            digest_value(document['phase_plan_sha256']),
            'invalid mechanism sequence or phase plan SHA256')
    require(type(document['trace_max_mib']) is int and
            document['trace_max_mib'] == 128,
            'mechanism service trace quota must be 128 MiB')
    require(type(max_len) is int and max_len == 262144,
            'mechanism diagnosis requires capacity 262144')
    group = document['group']
    require(type(group) is dict and set(group) == GROUP_FIELDS,
            'invalid mechanism group fields')
    index = group['index']
    require(type(index) is int and 1 <= index <= len(GROUP_ORDER),
            'invalid mechanism group index')
    condition, observation = GROUP_ORDER[index - 1]
    require(type(group['pair_index']) is int and
            group['pair_index'] == (index + 1) // 2 and
            type(group['pair_position']) is int and
            group['pair_position'] == 1 + (index - 1) % 2,
            'mechanism pair position differs')
    require(group['condition'] == condition and group['arm'] == condition[0]
            and group['predecessor'] == condition[1] and
            group['observation'] == observation,
            'mechanism condition or observation differs from fixed order')
    group_id = f'm{index:02d}-{condition.lower()}-{observation}'
    require(group['id'] == group_id, 'mechanism group ID differs from index')
    predecessor_tokens = 1024 if condition[1] == 'S' else 8193
    require(type(group['predecessor_tokens']) is int and
            group['predecessor_tokens'] == predecessor_tokens,
            'mechanism predecessor length differs')
    source_requests = group['requests']
    require(type(source_requests) is list and len(source_requests) == 4,
            'mechanism group requires exactly four ordered requests')
    requests = []
    for position, row in enumerate(source_requests):
        require(type(row) is dict and set(row) == REQUEST_FIELDS,
                'invalid mechanism request fields')
        role = 'conditioning' if position == 0 else 'probe'
        length = predecessor_tokens if position == 0 else 1024
        require(type(row['position']) is int and row['position'] == position
                and row['role'] == role, 'mechanism request role/order differs')
        require(type(row['input_tokens']) is int and
                row['input_tokens'] == length and
                type(row['output_tokens']) is int and
                row['output_tokens'] == 256,
                'mechanism input/output length differs')
        requests.append({
            'case': f'{group_id}-r{position:02d}-{role}-context-{length}',
            'position': position, 'role': role, 'input_tokens': length,
            'max_tokens': 256, 'total_tokens': length + 256, 'repeats': 1,
            'preceding_case': requests[-1]['case'] if requests else None,
            'preceding_input_tokens': (requests[-1]['input_tokens']
                                       if requests else None)})
    return {
        'mode': 'performance', 'scope': MECHANISM_SCOPE,
        'diagnostic_scope': MECHANISM_SCOPE,
        'group_id': group_id, 'group_index': index,
        'pair_index': group['pair_index'],
        'pair_position': group['pair_position'], 'condition': condition,
        'arm': group['arm'], 'predecessor': group['predecessor'],
        'predecessor_tokens': predecessor_tokens, 'observation': observation,
        'phase_diagnostics': observation == 'on',
        'trace_max_mib': document['trace_max_mib'],
        'sequence_sha256': sequence_sha256,
        'phase_plan_sha256': document['phase_plan_sha256'],
        'lengths': list(dict.fromkeys(r['input_tokens'] for r in requests)),
        'repeats': 1, 'requests': requests, 'partial': True,
        'partial_offload_matrix': True,
        'full_five_tier_requested': False,
        'full_offload_matrix_requested': False,
        'performance_acceptance': False,
        'client_protocol': 'one_evalscope_process_per_request',
        'client_protocol_note': 'One service; conditioning then three probes; '
            'no inter-request cache clearing. Observation on enables existing '
            'phase snapshots, residency timing and route trace together. '
            'Single service per cell; never performance acceptance.'}


def read_mechanism_sequence(path, expected_sha256, max_len=262144):
    """Validate and archive the same bytes, including duplicate-key rejection."""
    require(digest_value(expected_sha256), 'invalid expected mechanism SHA256')
    content = Path(path).read_bytes()
    require(hashlib.sha256(content).hexdigest() == expected_sha256,
            'mechanism sequence SHA256 differs')
    document = json.loads(content, object_pairs_hook=unique_object)
    return mechanism_plan(document, expected_sha256, max_len), content


def mechanism_environment(plan, inherited):
    """Build a clean environment without changing the old policy-axis rules."""
    require(plan.get('scope') == MECHANISM_SCOPE and
            plan.get('arm') in ('A', 'C') and
            plan.get('observation') in ('off', 'on'),
            'invalid mechanism plan identity')
    state = int(plan['arm'] == 'C')
    environment = {key: value for key, value in inherited.items()
                   if not key.startswith('Q4T_')}
    environment.update(policy_environment(
        0, state, 'request-partition-log', request_partition=state,
        decode_partition_log_quiet=state))
    if plan['observation'] == 'on':
        environment.update(DIAGNOSTIC_ENVIRONMENT)
    return environment


def check_mechanism_environment(plan, environment):
    expected = {key: value for key, value in
                mechanism_environment(plan, {}).items()}
    observed = {key: value for key, value in environment.items()
                if key.startswith('Q4T_')}
    require(observed == expected, 'mechanism arm/observation environment differs')


def mechanism_request_identity(plan, request):
    return {**{key: plan[key] for key in RESULT_FIELDS},
            **{key: request[key] for key in REQUEST_RESULT_FIELDS}}
