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


def startup_evidence(log, mtp, verifier='t4'):
    """Require the requested mode and unchanged fixed acceptance capacity."""
    effective, errors = _records(log, '[q4t][capabilities] effective ')
    capacity, capacity_errors = _records(log, '[q4t][capacity] ')
    errors.extend(capacity_errors)
    if verifier not in ('t4', 'sequential') or (not mtp and verifier != 't4'):
        errors.append('invalid requested verifier/mode')
    if len(effective) == 1:
        expected = verifier if mtp else 'none'
        # Missing fields are compatible only with the historical paths.
        actual = effective[0]['fields'].get('verifier',
                                           't4' if mtp else 'none')
        if actual != expected:
            errors.append(f'effective verifier={actual!r}, expected {expected!r}')
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
            'requested_verifier': verifier if mtp else 'none',
            'effective_capabilities': effective, 'capacity': capacity,
            'passed': not errors, 'errors': errors}


def sequential_fields_errors(fields):
    """Successful step accounting, also used for boundary cancellations.

    Mid-forward failures are not accepted by these HTTP success contracts.
    The runtime still logs their attempted forwards for diagnosis.
    """
    errors, counts = [], {}
    for key in ('mtp_steps', 'plain_tail_tokens', 'draft_forward_calls',
                'target_t1_calls', 'target_t4_calls', 'extend_forward_calls'):
        value = fields.get(key, '')
        if not re.fullmatch(r'[0-9]+', value):
            errors.append(f'invalid {key}')
        else:
            counts[key] = int(value)
    if fields.get('verifier') != 'sequential':
        errors.append('missing or wrong sequential verifier')
    if fields.get('forward_count_scope') != 'sequential_attempts':
        errors.append('missing or wrong forward count scope')
    reason = fields.get('tail_reason')
    if reason not in ('none', 'output_limit', 'context_limit'):
        errors.append('invalid tail reason')
    if len(counts) != 6:
        return errors
    steps, tail = counts['mtp_steps'], counts['plain_tail_tokens']
    if counts['draft_forward_calls'] != 2 * steps:
        errors.append('draft attempts differ from successful steps')
    if not steps <= counts['target_t1_calls'] <= 4 * steps:
        errors.append('target B1 attempts outside successful step bounds')
    if counts['target_t4_calls'] != 0:
        errors.append('sequential verifier performed T4 work')
    if not max(0, steps - 1) <= counts['extend_forward_calls'] <= steps:
        errors.append('invalid extend attempts for successful steps')
    if tail > 4 or (tail and reason == 'none'):
        errors.append('invalid sequential output tail')
    if tail and counts['extend_forward_calls'] != steps:
        errors.append('tail after a terminal step')
    return errors


def sequential_success_errors(fields, output, finish):
    """Bind completed HTTP output to consumed target inputs and pending stop."""
    errors = sequential_fields_errors(fields)
    if errors:
        return errors
    steps = int(fields['mtp_steps'])
    target = int(fields['target_t1_calls'])
    tail = int(fields['plain_tail_tokens'])
    extend = int(fields['extend_forward_calls'])
    if finish not in (['stop'], ['length']):
        errors.append('invalid successful finish reason')
    if steps and extend == steps - 1:
        if (tail != 0 or fields['tail_reason'] != 'none' or
                output != target + 1 or finish != ['stop']):
            errors.append('terminal target prefix/stop differs from HTTP output')
    elif fields.get('path') == 'prefill_only':
        if fields['tail_reason'] != 'none':
            errors.append('prefill-only request has a decode tail reason')
    else:
        if not tail or output != target + tail:
            errors.append('target prefix and ordinary tail differ from HTTP output')
    return errors


def request_mode_evidence(log, responses, mtp, verifier='t4'):
    """Bind terminal execution paths to real response IDs after server exit.

    Older plain baselines have no decode_path records. They remain usable when
    startup proves MTP is disabled. MTP runs must account for every response.
    """
    records, errors = _records(log, '[q4t][decode_path] ')
    if verifier not in ('t4', 'sequential') or (not mtp and verifier != 't4'):
        errors.append('invalid requested verifier/mode')
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
        if mtp and verifier == 'sequential':
            errors.extend(f'{response_id}: {error}'
                          for error in sequential_success_errors(
                              fields, output, response.get('finish')))
        elif fields.get('verifier', 't4' if mtp else 'none') != (
                verifier if mtp else 'none'):
            errors.append(f'{response_id}: verifier disagrees with runner')
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
            if path == 'plain_tail_b1':
                if steps != 0 or tail != output or not 0 < output <= 4:
                    errors.append(f'{response_id}: invalid pure ordinary B1 tail')
                # The new T4 tail contract must identify itself explicitly.
                # Missing-verifier historical T4 logs keep their old meaning.
                if verifier == 't4' and (
                        fields.get('verifier') != 't4' or
                        fields.get('tail_reason') not in (
                            'output_limit', 'context_limit')):
                    errors.append(f'{response_id}: unbound T4 ordinary tail')
            elif path != ('mtp_sequential_b1' if verifier == 'sequential'
                          else 'mtp_multi_b1') or steps <= 0:
                errors.append(f'{response_id}: no confirmed B=1 MTP execution')
        elif path != 'plain' or steps != 0 or tail != 0:
            errors.append(f'{response_id}: ordinary decode has unexpected MTP path')
        binding['coverage'] = 'terminal_request_path'
    if mtp and not responses:
        errors.append('no MTP responses to bind')
    return {'requested_decode_mode': 'mtp' if mtp else 'plain',
            'requested_verifier': verifier if mtp else 'none',
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
