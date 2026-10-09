"""Pure contracts for the link-wrapped five-request failure admission group."""
import re

from acceptance_mode import sequential_success_errors
from request_cancellation_contract import (parse_sse, require, response_fields,
                                            terminal_paths)

PLAN = (('failure-control', 'success'),
        ('failure-target2', 'target_call_2_in_request'),
        ('failure-target2-recovery', 'success'),
        ('failure-extend', 'first_extend'),
        ('failure-extend-recovery', 'success'))
VARIANT = ('[q4t][fault_variant] protocol=sequential_recovery_v1 '
           'production_binary=0 requests=5')
COUNTERS = ('q4t_requests_total', 'q4t_requests_success_total',
            'q4t_requests_error_total', 'q4t_requests_aborted_total')


def require_variant(log):
    markers = [line for line in log.splitlines()
               if line.startswith('[q4t][fault_variant]')]
    require(markers == [VARIANT], 'missing or wrong fault server variant')
    require('[q4t][fault_contract]' not in log, 'fault observer contract failed')


def failed_result(response, label):
    events = parse_sse(response, label)
    fields = response_fields(events)
    expected = {'message': 'generation failed', 'type': 'server_error',
                'code': 'generation_failed'}
    require(fields['errors'] == [expected] and events[-1].get('error') == expected,
            'expected exactly one final generation failure')
    require(not fields['finish'] and not fields['usage'],
            'failed request published normal finish/usage')
    return fields


def counters(body):
    values = []
    for name in COUNTERS:
        matches = re.findall(r'^' + name + r' ([0-9]+)$', body, re.MULTILINE)
        require(len(matches) == 1, 'missing or duplicate metric: ' + name)
        values.append(int(matches[0]))
    return tuple(values)


def require_counter_delta(before, after, success):
    expected = (1, int(success), int(not success), 0)
    require(tuple(b - a for a, b in zip(before, after)) == expected,
            'wrong failure recovery metrics delta')


def _events(log):
    found = []
    pattern = re.compile(r'^\[q4t\]\[fault_(begin|reset|injection|drain|end)\] (.*)$')
    for index, line in enumerate(log.splitlines()):
        match = pattern.fullmatch(line)
        if not match:
            continue
        pairs = [item.split('=', 1) for item in match[2].split()]
        require(all(len(pair) == 2 and all(pair) for pair in pairs),
                'malformed observer fields')
        row = dict(pairs)
        require(len(row) == len(pairs), 'duplicate observer field')
        require(re.fullmatch(r'[1-5]', row.get('ordinal', '')),
                'invalid observer request ordinal')
        found.append((match[1], row, index))
    return found


def validate_log(log):
    require_variant(log)
    paths = terminal_paths(log)
    require(list(paths) == [label for label, _ in PLAN],
            'expected exactly five actual response paths in serial order')
    events = _events(log)
    groups = []
    for ordinal, (label, kind) in enumerate(PLAN, 1):
        rows = [(event, row, line) for event, row, line in events
                if row['ordinal'] == str(ordinal)]
        expected = ['begin', 'reset', 'end'] if kind == 'success' else [
            'begin', 'reset', 'injection', 'drain', 'end']
        require([event for event, _, _ in rows] == expected,
                'missing or unordered fault/reset/drain/end: ' + label)
        require(all(row.get('real_ok') == '1' for _, row, _ in rows),
                'a wrapped real operation did not succeed')
        begin, end = rows[0][1], rows[-1][1]
        path_lines = [index for index, line in enumerate(log.splitlines())
                      if line.startswith('[q4t][decode_path] id=' + label + ' ')]
        require(len(path_lines) == 1 and
                rows[-2][2] < path_lines[0] < rows[-1][2],
                'actual response path is not inside its observed lifetime')
        for row, stage in [(begin, 'prefill'), (end, 'idle')]:
            require(all(row.get(key) == value for key, value in
                        {'stage': stage, 'position': '0', 'history': '0',
                         'pending': '0'}.items()), 'host state not cleared')
        require(begin.get('slot') == rows[1][1].get('slot') == '0',
                'reset used a different slot')
        require(rows[1][1].get('completion') == 'stream_ordered',
                'draft reset completion scope changed')
        path = paths[label]
        require(path.get('requested_mtp') == '1' and
                path.get('verifier') == 'sequential' and
                path.get('fallback') == 'none' and
                path.get('target_t4_calls') == '0' and
                path.get('forward_count_scope') == 'sequential_attempts',
                'wrong execution mode or fallback')
        for old, new in [('target_calls', 'target_t1_calls'),
                         ('draft_calls', 'draft_forward_calls'),
                         ('extend_calls', 'extend_forward_calls')]:
            require(re.fullmatch(r'[0-9]+', end.get(old, '')) and
                    end[old] == path.get(new), 'actual/core call counts differ')
        require(re.fullmatch(r'[0-9]+', end.get('tail_calls', '')) and
                re.fullmatch(r'[0-9]+', path.get('mtp_steps', '')),
                'invalid observed tail/step counts')
        if kind == 'success':
            require(end.get('injected') == end.get('drained') == '0',
                    'control or recovery was injected')
            require(path.get('path') == 'mtp_sequential_b1' and
                    int(path['mtp_steps']) > 0 and
                    path.get('tail_reason') == 'output_limit' and
                    not sequential_success_errors(path, 256, ['length']),
                    'successful request lacks strict output accounting')
            require(int(end['tail_calls']) ==
                    int(path['plain_tail_tokens']) - 1,
                    'ordinary tail call count differs from emitted tail')
        else:
            injection, drain = rows[2][1], rows[3][1]
            require(injection.get('kind') == kind and
                    injection.get('pending') == '0' and
                    re.fullmatch(r'[0-9]+', injection.get('position', '')) and
                    injection.get('position') == injection.get('history') and
                    drain.get('stream_match') == '1' and
                    drain.get('thread_match') == '1' and
                    end.get('injected') == end.get('drained') == '1',
                    'fault did not reach committed state plus production drain')
            for key in ('target_calls', 'draft_calls', 'extend_calls'):
                require(injection.get(key) == end[key],
                        'forward happened after injected failure')
            require(end['tail_calls'] == '0' and
                    path.get('plain_tail_tokens') == '0' and
                    path.get('tail_reason') == 'none',
                    'failed request entered ordinary tail')
            # No completed step means the existing path logger reports plain;
            # real target attempts and zero observed ordinary calls disambiguate.
            expected_path = ('mtp_sequential_b1' if int(path['mtp_steps']) else
                             'plain')
            require(path.get('path') == expected_path,
                    'unexpected failure path')
            if ordinal == 2:
                require(end['target_calls'] == '2', 'target call 2 not injected')
            else:
                require(end['extend_calls'] == '1' and
                        1 <= int(end['target_calls']) <= 4 and
                        end['draft_calls'] == '2' and path['mtp_steps'] == '0',
                        'first real extend not injected')
            errors = re.findall(r'^\[q4t\] generation failed id=' +
                                re.escape(label) + r'$', log, re.MULTILINE)
            require(len(errors) == 1, 'missing actual failed request ID')
        groups.append({'ordinal': ordinal, 'response_id': label,
                       'binding': 'five serial HTTP responses and path order',
                       'events': [row for _, row, _ in rows]})
    require([int(row['ordinal']) for _, row, _ in events] ==
            sorted(int(row['ordinal']) for _, row, _ in events),
            'observer request lifetimes overlap')
    failures = re.findall(r'^\[q4t\] generation failed id=(\S+)$', log,
                          re.MULTILINE)
    require(failures == [PLAN[1][0], PLAN[3][0]], 'unexpected failure IDs')
    control = {k: v for k, v in paths[PLAN[0][0]].items() if k != 'id'}
    for index in (2, 4):
        require({k: v for k, v in paths[PLAN[index][0]].items() if k != 'id'} ==
                control, 'recovery path differs from fresh same-process control')
    return {'groups': groups, 'terminal_paths': paths,
            'scope': 'logical forward Status failure; not CUDA fatal/OOM'}
