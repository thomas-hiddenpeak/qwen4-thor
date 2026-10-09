"""Bounded selection-control evidence; never natural T4 numerical admission."""
import hashlib
import json
import re

from request_cancellation_contract import (parse_sse, require, response_fields,
                                           terminal_paths)
from response_identity import response_identity

PLAN = tuple((f't4-stop-{row}', row, 256, row % 2 == 0)
             for row in range(4)) + tuple(
    (f't4-cap-{cap}', -1, cap, cap % 2 == 1) for cap in range(1, 6))
VARIANT = ('[q4t][t4_terminal_variant] protocol=t4_terminal_v1 '
           'production_binary=0 requests=9 selection_control=1')


def require_variant(log):
    markers = [line for line in log.splitlines()
               if line.startswith('[q4t][t4_terminal_variant]')]
    require(markers == [VARIANT], 'missing/wrong terminal variant')
    require('[q4t][t4_terminal_contract]' not in log,
            'terminal observer contract failed')
    require('[q4t][t4_fault_' not in log and '[q4t][fault_' not in log,
            'another link variant was used')


def completed_result(response, label, stop_row, cap, stream):
    require(response['status'] == 200, 'expected HTTP 200')
    if stream:
        events = parse_sse(response, label)
        fields = response_fields(events)
    else:
        require('data: ' not in response['body'], 'unexpected streaming body')
        event = json.loads(response['body'])
        require(isinstance(event, dict), 'invalid JSON response')
        identity = response_identity([event])
        require(identity['response_id_valid'] and
                identity['response_id'] == label, 'response identity differs')
        choices = event.get('choices')
        require(isinstance(choices, list) and len(choices) == 1 and
                isinstance(choices[0].get('message'), dict),
                'wrong nonstream choice shape')
        fields = {
            'text': choices[0]['message'].get('content'),
            'finish': [choices[0].get('finish_reason')],
            'usage': [event.get('usage')],
            'errors': [event['error']] if 'error' in event else []}
    output = stop_row + 2 if stop_row >= 0 else cap
    expected_usage = {'prompt_tokens': 1024, 'completion_tokens': output,
                      'total_tokens': 1024 + output}
    require(not fields['errors'] and isinstance(fields['text'], str),
            'controlled request failed')
    # Empty decoded text is valid for a selected special token. Usage and
    # production committed-position evidence carry token-count obligations.
    require(fields['finish'] == ['stop' if stop_row >= 0 else 'length'],
            'unexpected terminal reason / natural early EOS coverage failure')
    require(fields['usage'] == [expected_usage] and
            all(type(fields['usage'][0].get(key)) is int
                for key in expected_usage), 'unexpected token usage')
    fields['output_sha256'] = hashlib.sha256(fields['text'].encode()).hexdigest()
    return fields


def _events(log):
    found = []
    pattern = re.compile(r'^\[q4t\]\[t4_terminal_'
                         r'(begin|prefill|verify|select|restore|extend|return|end)'
                         r'\] (.*)$')
    for line_number, line in enumerate(log.splitlines()):
        match = pattern.fullmatch(line)
        if not match:
            require(not line.startswith('[q4t][t4_terminal_') or
                    line == VARIANT, 'unknown/malformed terminal event')
            continue
        pairs = [item.split('=', 1) for item in match[2].split()]
        require(all(len(pair) == 2 and all(pair) for pair in pairs),
                'malformed terminal observer fields')
        row = dict(pairs)
        require(len(row) == len(pairs), 'duplicate terminal observer field')
        require(re.fullmatch(r'[1-9]', row.get('ordinal', '')),
                'invalid terminal observer ordinal')
        require(row.get('real_ok') == '1', 'real operation failed')
        found.append((match[1], row, line_number))
    return found


def _integer(row, key, minimum=0):
    value = row.get(key, '')
    require(re.fullmatch(r'-?[0-9]+', value), 'invalid integer: ' + key)
    value = int(value)
    require(value >= minimum, 'integer below minimum: ' + key)
    return value


def _tokens(row, key, count):
    value = row.get(key, '')
    require(re.fullmatch(r'[0-9]+(?:,[0-9]+)*', value),
            'invalid token array: ' + key)
    values = [int(token) for token in value.split(',')]
    require(len(values) == count, 'wrong token array length: ' + key)
    return values


def validate_log(log):
    require_variant(log)
    require('generation failed id=' not in log and
            'request cancelled id=' not in log, 'generation failed/cancelled')
    events = _events(log)
    require([int(row['ordinal']) for _, row, _ in events] ==
            sorted(int(row['ordinal']) for _, row, _ in events),
            'request lifetimes overlap')
    paths = terminal_paths(log)
    require(list(paths) == [row[0] for row in PLAN],
            'missing/extra/unordered request paths')
    groups = []
    for ordinal, (label, stop_row, cap, stream) in enumerate(PLAN, 1):
        rows = [(event, row, line) for event, row, line in events
                if row['ordinal'] == str(ordinal)]
        expected = ['begin', 'prefill', 'end']
        controlled = stop_row >= 0 or cap == 5
        if controlled:
            expected = ['begin', 'prefill', 'verify', 'select']
            if 0 <= stop_row < 3:
                expected.append('restore')
            if cap == 5:
                expected.append('extend')
            expected += ['return', 'end']
        require([event for event, _, _ in rows] == expected,
                'missing/extra/unordered terminal events: ' + label)
        begin, end = rows[0][1], rows[-1][1]
        for row, stage in ((begin, 'prefill'), (end, 'idle')):
            require(all(row.get(key) == value for key, value in {
                'stage': stage, 'position': '0', 'history': '0',
                'pending': '0'}.items()), 'host state not clean')
        require(begin.get('slot') == '0', 'wrong sequence slot')
        require(all(rows[1][1].get(key) == value for key, value in {
            'rows': '1024', 'prompt_captured': '1',
            'completion': 'caller_owned'}.items()),
            'expected original prefill prompt observation')
        consumed = stop_row + 1 if stop_row >= 0 else cap - 1
        require(end.get('committed_position') ==
                end.get('committed_history') == str(1024 + consumed) and
                end.get('history_exact') == '1', 'wrong committed history')
        require(end.get('steps') == str(int(controlled)) and
                end.get('restores') == str(int(0 <= stop_row < 3)) and
                end.get('extends') == str(int(cap == 5)) and
                end.get('ordinary_calls') == str(cap - 1 if 1 <= cap <= 4
                                                  else 0),
                'wrong real forward/restore/extend counts')
        path = paths[label]
        path_lines = [index for index, line in enumerate(log.splitlines())
                      if line.startswith('[q4t][decode_path] id=' + label + ' ')]
        require(len(path_lines) == 1 and
                rows[-2][2] < path_lines[0] < rows[-1][2],
                'path outside request ownership lifetime')
        require(path.get('requested_mtp') == '1' and
                path.get('verifier') == 't4' and
                path.get('fallback') == 'none' and
                path.get('mtp_steps') == str(int(controlled)),
                'wrong real path / fallback')
        expected_path = ('mtp_multi_b1' if controlled else
                         'prefill_only' if cap == 1 else 'plain_tail_b1')
        require(path.get('path') == expected_path, 'wrong execution path')
        tail = 1 if cap == 5 else cap if 2 <= cap <= 4 else 0
        require(path.get('plain_tail_tokens') == str(tail),
                'wrong ordinary output tail count')
        if tail:
            require(path.get('tail_reason') == 'output_limit',
                    'ordinary tail not attributed to output quota')
        if controlled:
            by_kind = {event: row for event, row, _ in rows}
            verify, selected, returned = (by_kind[key] for key in
                                           ('verify', 'select', 'return'))
            require(all(verify.get(key) == value for key, value in {
                'batch': '1', 'rows': '4', 'position': '1024',
                'selected': '0'}.items()), 'not a real preselection T4 verify')
            require(_integer(selected, 'stop_row', -1) == stop_row and
                    _integer(selected, 'stop_count', 1) >= 1 and
                    selected.get('changed_after_real_d2h') == '1',
                    'selection not bound to configured stop / real readback')
            stop = _integer(selected, 'stop_id')
            bonus = _integer(selected, 'bonus')
            drafts = _tokens(selected, 'draft', 3)
            originals = _tokens(selected, 'original', 4)
            chosen = _tokens(selected, 'selected', 4)
            prefix = stop_row if stop_row >= 0 else 3
            require(chosen[:prefix] == drafts[:prefix] and
                    bonus != stop and stop not in drafts[:prefix],
                    'earlier configured stop / bad forced acceptance')
            expected_choice = originals.copy()
            expected_choice[:prefix] = drafts[:prefix]
            expected_choice[prefix] = stop if stop_row >= 0 else bonus
            require(chosen == expected_choice,
                    'extra changed argmax or wrong terminal row')
            require(all(returned.get(key) == value for key, value in {
                'counts': str(consumed), 'next_b': str(chosen[prefix]),
                'terminal': str(int(stop_row >= 0)), 'caller_unchanged': '1',
                'prefix_nonstop': '1', 'pending': '0',
                'restores': str(int(0 <= stop_row < 3)),
                'extends': str(int(cap == 5)), 'observer_cuda_calls': '0',
                'next_g': ('invalid_not_read' if stop_row >= 0
                           else 'valid_not_inspected')}.items()),
                'bad step publication contract')
            seed = _integer(returned, 'next_d0', -1)
            require(seed == -1 if stop_row >= 0 else seed >= 0,
                    'invalid next draft seed contract')
            if 0 <= stop_row < 3:
                require(by_kind['restore'].get('checkpoint') == str(stop_row)
                        and by_kind['restore'].get('slot') == '0',
                        'wrong checkpoint index')
            if cap == 5:
                require(by_kind['extend'].get('rows') == '4',
                        'cap5 did not extend four actual rows')
        groups.append({'ordinal': ordinal, 'request_id': label,
                       'stream': stream, 'events': [row for _, row, _ in rows],
                       'path': path})
    return {'passed': True, 'requests': 9, 'groups': groups,
            'scope': 'real T4 with controlled host selection and ordinary '
                     'tail; no natural quality, numerical or speed admission'}
