"""Frozen four-request predecessor diagnosis; never performance acceptance."""
import hashlib
import json
from pathlib import Path
import re

from offload_policy import BASE_ENVIRONMENT

CAUSALITY_SCOPE = 'offload_threshold_predecessor_v1'
BLOCK_ORDERS = (('AS', 'CS', 'CL', 'AL'), ('CS', 'AL', 'AS', 'CL'),
                ('AL', 'CL', 'CS', 'AS'), ('CL', 'AS', 'AL', 'CS'))
GROUP_FIELDS = {'id', 'block', 'block_position', 'condition', 'arm',
                'predecessor', 'predecessor_tokens', 'requests'}
REQUEST_FIELDS = {'position', 'role', 'input_tokens', 'output_tokens'}
RESULT_FIELDS = ('group_id', 'block', 'block_position', 'condition', 'arm',
                 'predecessor', 'predecessor_tokens', 'sequence_sha256',
                 'phase_plan_sha256')
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
        require(key not in value, 'duplicate sequence JSON key: ' + key)
        value[key] = item
    return value


def causality_plan(document, sequence_sha256, max_len=262144):
    """Validate a bounded group document, deriving rather than trusting cases."""
    require(type(document) is dict and set(document) ==
            {'schema', 'scope', 'phase_plan_sha256', 'group'},
            'invalid causality document fields')
    require(type(document['schema']) is int and document['schema'] == 1 and
            document['scope'] == CAUSALITY_SCOPE,
            'unsupported causality schema or scope')
    require(digest_value(sequence_sha256) and
            digest_value(document['phase_plan_sha256']),
            'invalid sequence or phase plan SHA256')
    require(type(max_len) is int and max_len == 262144,
            'causality diagnosis requires capacity 262144')
    group = document['group']
    require(type(group) is dict and set(group) == GROUP_FIELDS,
            'invalid causality group fields')
    for key in ('block', 'block_position'):
        require(type(group[key]) is int and 1 <= group[key] <= 4,
                'invalid causality ' + key)
    condition = BLOCK_ORDERS[group['block'] - 1][group['block_position'] - 1]
    require(group['condition'] == condition and group['arm'] == condition[0]
            and group['predecessor'] == condition[1],
            'causality condition/arm/predecessor differs from Williams order')
    group_id = (f"b{group['block']:02d}-p{group['block_position']:02d}-"
                f'{condition.lower()}')
    require(group['id'] == group_id, 'causality group ID differs from position')
    predecessor_tokens = 1024 if condition[1] == 'S' else 8193
    require(type(group['predecessor_tokens']) is int and
            group['predecessor_tokens'] == predecessor_tokens,
            'causality predecessor length differs')
    source_requests = group['requests']
    require(type(source_requests) is list and len(source_requests) == 4,
            'causality group requires exactly four ordered requests')
    requests = []
    for position, row in enumerate(source_requests):
        require(type(row) is dict and set(row) == REQUEST_FIELDS,
                'invalid causality request fields')
        role = 'conditioning' if position == 0 else 'probe'
        length = predecessor_tokens if position == 0 else 1024
        require(type(row['position']) is int and row['position'] == position
                and row['role'] == role, 'causality request role/order differs')
        require(type(row['input_tokens']) is int and
                row['input_tokens'] == length and
                type(row['output_tokens']) is int and
                row['output_tokens'] == 256,
                'causality input/output length differs')
        requests.append({
            'case': f'{group_id}-r{position:02d}-{role}-context-{length}',
            'position': position, 'role': role, 'input_tokens': length,
            'max_tokens': 256, 'total_tokens': length + 256, 'repeats': 1,
            'preceding_case': requests[-1]['case'] if requests else None,
            'preceding_input_tokens': (requests[-1]['input_tokens']
                                       if requests else None)})
    return {
        'mode': 'performance', 'scope': CAUSALITY_SCOPE,
        'diagnostic_scope': CAUSALITY_SCOPE,
        'group_id': group_id, 'block': group['block'],
        'block_position': group['block_position'], 'condition': condition,
        'arm': group['arm'], 'predecessor': group['predecessor'],
        'predecessor_tokens': predecessor_tokens,
        'sequence_sha256': sequence_sha256,
        'phase_plan_sha256': document['phase_plan_sha256'],
        'lengths': list(dict.fromkeys(r['input_tokens'] for r in requests)),
        'repeats': 1, 'requests': requests, 'partial': True,
        'partial_offload_matrix': True,
        'full_five_tier_requested': False,
        'full_offload_matrix_requested': False,
        'performance_acceptance': False,
        'client_protocol': 'one_evalscope_process_per_request',
        'client_protocol_note': 'One service; one conditioning request and '
                                'three ordered probes; no cache clearing '
                                'between requests. Never matrix acceptance.'}


def read_causality_sequence(path, expected_sha256, max_len=262144):
    """Read bytes once; the same validated bytes must be archived by callers."""
    require(digest_value(expected_sha256), 'invalid expected sequence SHA256')
    content = Path(path).read_bytes()
    require(hashlib.sha256(content).hexdigest() == expected_sha256,
            'causality sequence SHA256 differs')
    document = json.loads(content, object_pairs_hook=unique_object)
    return causality_plan(document, expected_sha256, max_len), content


def load_causality_sequence(path, expected_sha256, max_len=262144):
    return read_causality_sequence(path, expected_sha256, max_len)[0]


def check_causality_environment(plan, environment):
    """Prevent a valid sequence from mislabelling the runtime intervention."""
    require(plan.get('scope') == CAUSALITY_SCOPE and
            plan.get('arm') in ('A', 'C'), 'invalid causality plan identity')
    state = '0' if plan['arm'] == 'A' else '1'
    expected = {**BASE_ENVIRONMENT, 'Q4T_MOE_CHUNK_ORDER': '0',
                'Q4T_MOE_PARTITION': state, 'Q4T_MOE_REQUEST_PARTITION': state,
                'Q4T_MOE_DECODE_PARTITION_LOG_QUIET': state}
    require(all(environment.get(key) == value
                for key, value in expected.items()),
            'causality arm/environment differs')
    require(all(environment.get(key) in (None, '0') for key in
                ('Q4T_OFFLOAD_PHASE_DIAGNOSTICS', 'Q4T_RESIDENCY_TIMING')),
            'causality diagnosis must not enable runtime instrumentation')


def causality_request_identity(plan, request):
    return {**{key: plan[key] for key in RESULT_FIELDS},
            **{key: request[key] for key in REQUEST_RESULT_FIELDS}}
