"""Pure contracts for the bounded same-mode cancellation scenario.

No service, socket, model, or timing measurements are started by this module.
Historical ordinary/MTP comparison scopes retain their separate contracts.
"""
import hashlib
import json
import re

from response_identity import response_identity

COUNTERS = ('q4t_requests_total', 'q4t_requests_aborted_total',
            'q4t_requests_success_total')
CONTROL = 'same-control'
RECOVERIES = ('same-prefill-recovery', 'same-decode-recovery',
              'same-rst-recovery', 'same-deadline-recovery')
INTERRUPTS = {'same-prefill-cancel': 'explicit_cancel',
              'same-decode-cancel': 'explicit_cancel',
              'same-decode-rst': 'client_disconnected',
              'same-decode-deadline': 'deadline_exceeded'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def recovery_plan():
    return [
        (CONTROL, 'control', 'decode'),
        ('same-prefill-cancel', 'prefill_cancel', 'long'),
        (RECOVERIES[0], 'recovery', 'decode'),
        ('same-decode-cancel', 'explicit', 'decode'),
        (RECOVERIES[1], 'recovery', 'decode'),
        ('same-decode-rst', 'rst', 'decode'),
        (RECOVERIES[2], 'recovery', 'decode'),
        ('same-decode-deadline', 'deadline', 'decode'),
        (RECOVERIES[3], 'recovery', 'decode'),
    ]


def select_fixtures(quality_inputs, manifest, performance_inputs, results):
    """Use frozen input metadata only; historical outputs are never an oracle."""
    long_rows = [row for row in manifest if row.get('length') == 45056]
    require(long_rows, 'missing 44K fixture metadata')
    selected = long_rows[0]  # Frozen manifest order; no answer-based selection.
    long_inputs = [row['prompt'] for row in quality_inputs
                   if isinstance(row.get('prompt'), str) and
                   hashlib.sha256(row['prompt'].encode()).hexdigest() ==
                   selected['prompt_sha256']]
    require(len(long_inputs) == 1, 'missing or ambiguous 44K input')
    short_rows = [row for row in results if row.get('length') == 1024]
    require(len(short_rows) == 1, 'missing or ambiguous 1K fixture metadata')
    prompts = [row.get('prompt') for row in performance_inputs]
    require(prompts and all(isinstance(prompt, str) and prompt for prompt in prompts)
            and len(set(prompts)) == 1, 'ambiguous 1K input')
    short_hash = hashlib.sha256(prompts[0].encode()).hexdigest()
    require(short_hash == short_rows[0]['prompt_sha256'], '1K input hash mismatch')
    return ({'long': long_inputs[0], 'decode': prompts[0]},
            {'long': {'length': 45056, 'manifest_id': selected['id'],
                      'prompt_sha256': selected['prompt_sha256']},
             'decode': {'length': 1024, 'prompt_sha256': short_hash}})


def parse_sse(response, label, complete=True):
    require(response['status'] == 200, 'expected streaming HTTP 200')
    lines = response['body'].splitlines(keepends=True)
    data = [line[6:].rstrip('\r\n') for line in lines
            if line.startswith('data: ') and
            (complete or line.endswith('\n'))]
    if complete:
        require(data and data[-1] == '[DONE]' and data.count('[DONE]') == 1,
                'expected exactly one terminal DONE')
        data = data[:-1]
    else:
        require('[DONE]' not in data, 'completed before interruption')
    parsed = [json.loads(line) for line in data]
    require(all(isinstance(event, dict) for event in parsed), 'invalid SSE event')
    identity = response_identity(parsed)
    require(identity['response_id_valid'] and identity['response_id'] == label,
            'missing or inconsistent actual response ID')
    return parsed


def response_fields(events):
    choices = [choice for event in events for choice in event.get('choices', [])]
    return {
        'text': ''.join(choice.get('delta', {}).get('content', '')
                        for choice in choices),
        'finish': [choice['finish_reason'] for choice in choices
                   if choice.get('finish_reason')],
        'usage': [event['usage'] for event in events
                  if event.get('usage') is not None],
        'errors': [event['error'] for event in events if 'error' in event],
    }


def completed_result(response, label):
    result = response_fields(parse_sse(response, label))
    require(result['text'] and not result['errors'], 'control/recovery failed')
    require(result['finish'] == ['length'], 'unexpected normal finish')
    expected_usage = {'prompt_tokens': 1024, 'completion_tokens': 256,
                      'total_tokens': 1280}
    require(len(result['usage']) == 1, 'expected one usage record')
    usage = result['usage'][0]
    require(usage == expected_usage and
            all(type(usage.get(key)) is int for key in expected_usage),
            'unexpected token usage')
    result['output_sha256'] = hashlib.sha256(result['text'].encode()).hexdigest()
    return result


def require_recovery(actual, control):
    require(control is not None, 'missing fresh same-mode control')
    require(actual == control, 'recovery differs from fresh same-mode control')


def require_cancelled(response, label):
    events = parse_sse(response, label)
    result = response_fields(events)
    require(result['text'], 'no content before decode interruption')
    require(len(result['errors']) == 1 and
            result['errors'][0].get('message') == 'request cancelled',
            'expected one cancellation error')
    require(not result['finish'] and not result['usage'],
            'cancellation contains normal completion')
    require('error' in events[-1] and
            response_fields(events[:-1])['text'],
            'cancellation error must follow content and be the last event')
    return result


def require_partial(response, label):
    result = response_fields(parse_sse(response, label, complete=False))
    require(result['text'] and not result['errors'] and
            not result['finish'] and not result['usage'],
            'RST must follow content and precede a terminal response')
    return result


def metric_values(body):
    values = {}
    for name in COUNTERS:
        matches = re.findall(r'^' + name + r' ([0-9]+)$', body, re.MULTILINE)
        require(len(matches) == 1, 'missing or duplicate metric: ' + name)
        values[name] = int(matches[0])
    return values


def require_delta(before, after, expected):
    actual = tuple(after[name] - before[name] for name in COUNTERS)
    require(actual == expected, 'request counter delta: ' + repr(actual))


def require_prefill_cancel(log):
    progress = r'^\[q4t\] prefill cancelled seq=0 position=([0-9]+) total=45056$'
    matches = re.findall(progress, log, re.MULTILINE)
    require(len(matches) == 1 and 0 < int(matches[0]) < 45056,
            'missing bounded 44K prefill progress')
    paired = re.findall(progress + r'\n\[q4t\] request cancelled '
                         r'id=same-prefill-cancel reason=explicit_cancel$',
                         log, re.MULTILINE)
    require(paired == matches, 'prefill progress is not bound to its request ID')
    return int(matches[0])


def require_prefill_response(response):
    require(response['status'] == 409 and 'data:' not in response['body'],
            'expected prefill HTTP 409 without SSE')
    require(json.loads(response['body'])['error']['message'] ==
            'request cancelled during prefill', 'wrong prefill cancellation error')


def read_body(response, received, first=None, stop_after_content=False,
              limit=4 * 1024 * 1024, expired=lambda: False):
    """Read HTTPResponse-decoded bytes; a caller-owned deadline closes its socket.

    read1 preserves HTTP framing and limits each buffered read. The separate
    deadline also interrupts slow/dripping headers or individual SSE lines.
    """
    pending = bytearray()
    while True:
        require(not expired(), 'HTTP total deadline exceeded')
        chunk = response.read1(65536)
        received.extend(chunk)
        require(not expired(), 'HTTP total deadline exceeded')
        if not chunk:
            require(response.length in (None, 0), 'truncated HTTP response body')
            return len(received)
        require(len(received) <= limit, 'HTTP response byte limit exceeded')
        pending.extend(chunk)
        saw_content = False
        while b'\n' in pending:
            line, _, rest = pending.partition(b'\n')
            pending = bytearray(rest)
            if line.startswith(b'data: {'):
                event = json.loads(line[6:])
                if any(choice.get('delta', {}).get('content')
                       for choice in event.get('choices', [])):
                    saw_content = True
                    if first:
                        first.set()
        if stop_after_content and saw_content:
            # A read can also contain half of the next SSE line/UTF-8 codepoint.
            # Preserve those bytes, but return only the complete line prefix.
            return len(received) - len(pending)


def terminal_paths(log):
    rows = {}
    prefix = '[q4t][decode_path] '
    for line in log.splitlines():
        if not line.startswith(prefix):
            continue
        pairs = [field.split('=', 1) for field in line[len(prefix):].split()]
        require(all(len(pair) == 2 and all(pair) for pair in pairs),
                'malformed terminal path')
        fields = dict(pairs)
        require(len(fields) == len(pairs), 'duplicate terminal field')
        label = fields.get('id')
        require(label and label not in rows, 'missing or duplicate path ID')
        rows[label] = fields
    return rows


def validate_same_mode_paths(log):
    rows = terminal_paths(log)
    expected = {label for label, kind, _ in recovery_plan()
                if kind != 'prefill_cancel'}
    require(set(rows) == expected, 'expected exactly eight decode paths')
    for label, row in rows.items():
        require(row.get('requested_mtp') == '1' and
                row.get('path') == 'mtp_multi_b1' and
                re.fullmatch(r'[1-9][0-9]*', row.get('mtp_steps', '')) and
                row.get('fallback') == 'none' and
                row.get('plain_tail_tokens') == '0',
                'not the frozen MTP path: ' + label)
    steps = rows[CONTROL]['mtp_steps']
    require(all(rows[label]['mtp_steps'] == steps for label in RECOVERIES),
            'recovery MTP steps differ from fresh control')
    for label, reason in INTERRUPTS.items():
        found = re.findall(r'^\[q4t\] request cancelled id=' + re.escape(label) +
                           r' reason=([^\s]+)$', log, re.MULTILINE)
        require(found == [reason], 'missing or wrong cancellation reason: ' + label)
    position = require_prefill_cancel(log)
    return {'terminal_paths': rows, 'prefill_cancel_position': position,
            'control_mtp_steps': int(steps)}
