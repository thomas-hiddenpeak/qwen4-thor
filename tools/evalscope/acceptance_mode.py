"""Strict mode evidence for the existing single-stream HTTP runner."""
import math
import re


def _records(log, prefix):
    records, errors = [], []
    for line in log.splitlines():
        if not line.startswith(prefix):
            continue
        fields = {}
        for item in line[len(prefix):].split():
            key, separator, value = item.partition('=')
            if not separator or not key or not value or key in fields:
                errors.append(f'malformed or duplicate field in {line}')
                break
            fields[key] = value
        else:
            records.append({'fields': fields, 'raw': line})
    return records, errors


def startup_evidence(log, mtp):
    """Require the requested mode and unchanged fixed acceptance capacity."""
    effective, errors = _records(log, '[q4t][capabilities] effective ')
    capacity, capacity_errors = _records(log, '[q4t][capacity] ')
    errors.extend(capacity_errors)
    checks = [
        ('effective capabilities', effective,
         {'mtp': str(int(mtp)), 'media_allowed': '0', 'vision_loaded': '0',
          'max_seq': '1'}),
        ('capacity', capacity,
         {'requested_max_len': '208896', 'requested_max_seq': '1',
          'requested_max_prefill': '8192', 'effective_max_len': '208896',
          'effective_max_seq': '1', 'effective_max_prefill': '8192',
          'budget_enabled': '1', 'budget_feasible': 'true'}),
    ]
    for label, records, expected in checks:
        if len(records) != 1:
            errors.append(f'expected one {label} record, got {len(records)}')
            continue
        for key, value in expected.items():
            actual = records[0]['fields'].get(key)
            if actual != value:
                errors.append(f'{label}: {key}={actual!r}, expected {value!r}')
    return {'requested_decode_mode': 'mtp' if mtp else 'plain',
            'effective_capabilities': effective, 'capacity': capacity,
            'passed': not errors, 'errors': errors}


def request_mode_evidence(log, responses, mtp):
    """Bind terminal execution paths to real response IDs after server exit.

    Older plain baselines have no decode_path records. They remain usable when
    startup proves MTP is disabled. MTP runs must account for every response.
    """
    records, errors = _records(log, '[q4t][decode_path] ')
    by_id = {}
    for record in records:
        by_id.setdefault(record['fields'].get('id'), []).append(record)
    expected_ids = [row.get('response_id') for row in responses]
    if len(set(expected_ids)) != len(expected_ids) or None in expected_ids:
        errors.append('responses have missing or duplicate request identities')
    unexpected = set(by_id) - set(expected_ids)
    if unexpected:
        errors.append(f'unmatched terminal request IDs: {sorted(map(str, unexpected))}')
    bindings = []
    for response in responses:
        response_id = response.get('response_id')
        output = response['actual_output']
        found = by_id.get(response_id, [])
        binding = {'response_id': response_id, 'records': found,
                   'actual_output': output}
        bindings.append(binding)
        if isinstance(output, bool) or not isinstance(output, int) or output < 0:
            errors.append(f'{response_id}: invalid actual output count')
            continue
        if not mtp and not records:
            binding['coverage'] = 'legacy_plain_startup_only'
            continue
        if len(found) != 1:
            errors.append(f'{response_id}: expected one terminal path, got {len(found)}')
            continue
        fields = found[0]['fields']
        if fields.get('requested_mtp') != str(int(mtp)):
            errors.append(f'{response_id}: requested_mtp disagrees with runner')
        counts = {}
        for key in ['mtp_steps', 'plain_tail_tokens']:
            value = fields.get(key, '')
            if not re.fullmatch(r'[0-9]+', value):
                errors.append(f'{response_id}: invalid {key}')
            else:
                counts[key] = int(value)
        if len(counts) != 2:
            continue
        steps, tail = counts['mtp_steps'], counts['plain_tail_tokens']
        if tail > output:
            errors.append(f'{response_id}: tail exceeds actual output count')
        path, fallback = fields.get('path'), fields.get('fallback')
        if fallback != 'none':
            errors.append(f'{response_id}: fallback={fallback!r}')
        if path == 'prefill_only':
            if steps != 0 or tail != 0 or not 0 < output <= 1:
                errors.append(f'{response_id}: prefill_only has decode work')
        elif mtp:
            if path != 'mtp_multi_b1' or steps <= 0:
                errors.append(f'{response_id}: no confirmed B=1 MTP execution')
        elif path != 'plain' or steps != 0 or tail != 0:
            errors.append(f'{response_id}: ordinary decode has unexpected MTP path')
        binding['coverage'] = 'terminal_request_path'
    if mtp and not responses:
        errors.append('no MTP responses to bind')
    return {'requested_decode_mode': 'mtp' if mtp else 'plain',
            'bindings': bindings, 'passed': not errors, 'errors': errors}


def performance_metrics(row):
    """Keep evalscope's client timing; do not infer per-token arrival times."""
    ttft, latency = row['ttft'], row['latency']
    for name, value in [('ttft', ttft), ('latency', latency)]:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f'invalid evalscope {name}')
    if ttft < 0 or latency <= ttft or row['actual_output'] <= 1:
        raise ValueError('invalid streaming performance interval/output count')
    return {'ttft': ttft, 'latency': latency,
            'decode_seconds': latency - ttft,
            'decode_tps': (row['actual_output'] - 1) / (latency - ttft),
            'overall_tps': row['actual_output'] / latency,
            'actual_output': row['actual_output']}
