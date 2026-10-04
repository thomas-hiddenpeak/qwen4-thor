"""Frozen request-length policy checks; no inference or acceptance by itself."""
import hashlib
import math
import re

POLICY_AXIS = 'request-partition'
POLICY_ENV = 'Q4T_MOE_REQUEST_PARTITION'
THRESHOLD = 8192
SEQUENCE = (16385, 8192, 8193, 1024, 45056, 4096, 8192)
SEQUENCE_ROUNDS = 3
SEQUENCE_SCOPE = 'request_partition_history_v1'
MATRIX_LENGTHS = (1024, 4096, 8192, 45056, 204800, 261887)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sequence_plan(max_len=262144):
    require(type(max_len) is int and max_len >= max(SEQUENCE) + 256,
            'sequence capacity cannot contain its fixed requests')
    requests = []
    for round_index in range(SEQUENCE_ROUNDS):
        for position, length in enumerate(SEQUENCE):
            requests.append({
                'case': f'sequence-r{round_index + 1:02d}-p{position + 1:02d}'
                        f'-context-{length}',
                'round': round_index + 1, 'position': position + 1,
                'input_tokens': length, 'max_tokens': 256,
                'total_tokens': length + 256, 'repeats': 1})
    return {'mode': 'performance', 'lengths': list(dict.fromkeys(SEQUENCE)),
            'sequence': list(SEQUENCE), 'rounds': SEQUENCE_ROUNDS,
            'repeats': SEQUENCE_ROUNDS, 'requests': requests,
            'partial': True, 'partial_offload_matrix': True,
            'scope': SEQUENCE_SCOPE,
            'full_five_tier_requested': False,
            'full_offload_matrix_requested': False,
            'performance_acceptance': False,
            'client_protocol': 'one_evalscope_process_per_request',
            'client_protocol_note': 'One service; no clearing between requests. '
                                    'Three fixed rounds; positions remain separate.'}


def sequence_metrics(baseline, candidate):
    """Apply unchanged observed-range gates at each frozen history position."""
    expected = sequence_plan()['requests']
    require(len(baseline) == len(candidate) == len(expected),
            'history requires all 21 ordered requests per arm')
    by_position = {position: ([], []) for position in range(1, len(SEQUENCE) + 1)}
    for group_index, rows in enumerate((baseline, candidate)):
        for row, request in zip(rows, expected):
            require(all(row.get(k) == request[k]
                        for k in ('case', 'round', 'position', 'input_tokens')),
                    'history order/position differs from frozen sequence')
            require(all(type(row.get(k)) in (int, float) and
                        math.isfinite(row[k]) and row[k] > 0
                        for k in ('ttft', 'decode_tps')), 'invalid history metric')
            by_position[row['position']][group_index].append(row)
    positions = []
    for position, (before, after) in by_position.items():
        checks = {
            'first_of_position_ttft': after[0]['ttft'] <= before[0]['ttft'],
            'later_ttft': max(r['ttft'] for r in after[1:]) <=
                          max(r['ttft'] for r in before[1:]),
            'decode': min(r['decode_tps'] for r in after) >=
                      min(r['decode_tps'] for r in before)}
        positions.append({'position': position,
                          'input_tokens': SEQUENCE[position - 1],
                          'checks': checks, 'passed': all(checks.values()),
                          'baseline': before, 'candidate': after})
    return {'scope': SEQUENCE_SCOPE, 'positions': positions,
            'passed': all(p['passed'] for p in positions),
            'performance_acceptance': False,
            'inference_limit': 'Three observed values per history position; '
                               'no statistical confidence or tail claim.'}


def request_path_evidence(log, requests, state):
    """Bind actual layer-zero execution to HTTP IDs and all prompt chunks.

    Layer zero proves context propagation to execution, not all intermediate
    numerical values. Existing per-layer partition logs remain separate evidence.
    """
    require(type(state) is int and state in (0, 1), 'invalid policy state')
    require(isinstance(requests, list) and requests, 'no HTTP requests to audit')
    lines = [line for line in log.splitlines()
             if '[q4t][request_partition]' in line]
    pattern = re.compile(
        r'\[q4t\]\[request_partition\] request=(\S+) input_tokens=(\d+) '
        r'active=([01]) partition=([01]) base=(\d+) tokens=(\d+) layer=0 applied=([01]) '
        r'fallback=([01]) reason=(\w+)(?:\s|$)')
    rows = []
    for line in lines:
        match = pattern.search(line)
        require(match is not None, 'malformed request policy execution record')
        request_id, *fields, reason = match.groups()
        total, active, policy, base, tokens, applied, fallback = map(int, fields)
        rows.append(dict(request_id=request_id, input_tokens=total,
                         active=active, partition=policy, base=base, tokens=tokens,
                         applied=applied, fallback=fallback, reason=reason))
    expected = []
    seen = set()
    for request in requests:
        request_id = request['response_id']
        length = request['actual_input']
        require(request.get('response_id_valid') is True and
                isinstance(request_id, str) and request_id and
                request_id not in seen and type(length) is int and length > 0,
                'HTTP identity/input invalid or duplicated')
        seen.add(request_id)
        policy = int(state == 1 and length > THRESHOLD)
        for base in range(0, length, THRESHOLD):
            tokens = min(THRESHOLD, length - base)
            applied = int(policy == 1 and tokens > 1)
            reason = ('global_legacy' if not state else
                      'request_legacy' if not policy else
                      'candidate' if applied else 'prefill_singleton')
            expected.append(dict(request_id=request_id, input_tokens=length,
                                 active=state, partition=policy, base=base, tokens=tokens,
                                 applied=applied, fallback=0, reason=reason))
    require(rows == expected,
            'request policy execution differs from full request/chunk contract')
    return {'request_partition': state, 'execution_records': len(rows),
            'request_count': len(requests), 'runtime_eligible': True,
            'selected_legacy_requests': sum(state == 0 or r['actual_input'] <= THRESHOLD
                                            for r in requests),
            'candidate_chunks': sum(r['applied'] for r in rows),
            'singleton_tail_chunks': sum(r['reason'] == 'prefill_singleton'
                                         for r in rows),
            'numerical_acceptance': False,
            'scope': 'Actual layer-zero prefill execution; no decode or '
                     'all-intermediate numerical proof.'}


def all_layer_partition_evidence(log, requests, state):
    """Require exact serial request/chunk/layer and true decode accounting.

    Full request marker precedes layer-zero's existing partition record. Each
    selected prefill chunk must contain all 48 layers in order. Decode is
    identified by its position after that request's complete prefill plus its
    exact output-token count; T=1 alone is never treated as a phase label.
    """
    request_path_evidence(log, requests, state)
    events = [line for line in log.splitlines()
              if '[q4t][request_partition]' in line or
              '[q4t][residency][partition]' in line]
    pattern = re.compile(
        r'\[q4t\]\[residency\]\[partition\] layer=(\d+) T=(\d+) '
        r'policy=min_new_csr_v1 requested=1 applied=([01]) fallback=([01]) '
        r'reason=(\w+) chunks=(\d+) singleton_chunks=(\d+) '
        r'work_used=(\d+) work_budget=(\d+) metadata_bytes=(\d+)(?:\s|$)')
    cursor = 0
    counts = dict(candidate_prefill_layer_forwards=0,
                  singleton_prefill_layer_forwards=0, decode_layer_forwards=0)

    def consume_layers(tokens, reason):
        nonlocal cursor
        for layer in range(48):
            require(cursor < len(events), 'missing partition layer execution')
            match = pattern.search(events[cursor])
            require(match is not None, 'partition layer interrupted or malformed')
            actual_layer, actual_tokens, applied, fallback = map(
                int, match.groups()[:4])
            chunks, singleton, work, budget, metadata = map(
                int, match.groups()[5:])
            require(actual_layer == layer and actual_tokens == tokens and
                    match[5] == reason and fallback == 0,
                    'partition request/chunk/layer/phase mismatch or fallback')
            require(0 < chunks <= tokens and 0 <= singleton <= chunks and
                    metadata >= 0, 'invalid partition dimensions')
            if reason == 'candidate':
                require(applied == 1 and tokens > 1 and
                        budget == 32 * tokens * 10 and 0 < work <= budget,
                        'candidate planning contract failed')
                counts['candidate_prefill_layer_forwards'] += 1
            else:
                require(applied == 0 and tokens == chunks == singleton == 1 and
                        work == budget == metadata == 0,
                        'singleton/decode execution contract failed')
                key = ('singleton_prefill_layer_forwards'
                       if reason == 'prefill_singleton' else
                       'decode_layer_forwards')
                counts[key] += 1
            cursor += 1

    for request in requests:
        length = request['actual_input']
        output = request['actual_output']
        require(type(output) is int and output > 0, 'invalid actual output count')
        for base in range(0, length, THRESHOLD):
            require(cursor < len(events) and
                    '[q4t][request_partition]' in events[cursor],
                    'missing prefill execution marker or unexpected partition')
            cursor += 1
            if state == 1 and length > THRESHOLD:
                tokens = min(THRESHOLD, length - base)
                consume_layers(tokens, 'candidate' if tokens > 1
                               else 'prefill_singleton')
        if state == 1:
            for _ in range(output - 1):
                consume_layers(1, 'decode')
    require(cursor == len(events), 'unexpected trailing partition execution')
    return {'runtime_eligible': True, 'all_layers': 48, **counts,
            'budget_fallbacks': 0, 'unsupported_forwards': 0,
            'scope': 'Exact serial execution logs for all prefill chunks and '
                     'output-minus-one decode forwards; no numerical proof.',
            'numerical_acceptance': False}


def sequence_output_evidence(requests):
    """Same prompt must retain identical text across rounds and positions."""
    expected = sequence_plan()['requests']
    require(len(requests) == len(expected), 'history output audit needs 21 requests')
    outputs = {}
    occurrences = {}
    for row, request in zip(requests, expected):
        require(row['actual_input'] == request['input_tokens'] and
                row['actual_output'] == request['max_tokens'] and
                row['finish'] == ['length'] and row['success'] and
                isinstance(row['text'], str), 'history output/request mismatch')
        prompt = row['prompt_sha256']
        require(isinstance(prompt, str) and re.fullmatch(r'[0-9a-f]{64}', prompt),
                'history prompt identity missing')
        digest = hashlib.sha256(row['text'].encode()).hexdigest()
        require(outputs.setdefault(prompt, digest) == digest,
                'history output changed across repetitions or positions')
        occurrences[prompt] = occurrences.get(prompt, 0) + 1
    require(len(outputs) == len(set(SEQUENCE)),
            'history does not contain one fixed prompt for each frozen length')
    return {'passed': True, 'request_count': len(requests),
            'unique_prompt_count': len(outputs), 'output_sha256': outputs,
            'prompt_occurrences': occurrences, 'numerical_acceptance': False}
