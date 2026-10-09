"""Nine-request real T4 logical-failure contract; no numerical admission claim."""
import re

from mtp_failure_recovery_contract import (COUNTERS, failed_result,
                                          require_counter_delta)
from request_cancellation_contract import require, terminal_paths

PLAN = (('t4-control', 'success'),
        ('t4-upload', 'verify_sequence_upload'),
        ('t4-upload-recovery', 'success'),
        ('t4-verify', 'verify_return'),
        ('t4-verify-recovery', 'success'),
        ('t4-restore', 'natural_restore'),
        ('t4-restore-recovery', 'success'),
        ('t4-extend', 'extend_attention'),
        ('t4-extend-recovery', 'success'))
VARIANT = ('[q4t][t4_fault_variant] protocol=t4_completion_v1 '
           'production_binary=0 requests=9')


def require_variant(log):
    markers = [line for line in log.splitlines()
               if line.startswith('[q4t][t4_fault_variant]')]
    require(markers == [VARIANT], 'missing/wrong T4 variant marker')
    require('[q4t][t4_fault_contract]' not in log, 'T4 observer contract failed')
    require('[q4t][fault_variant]' not in log, 'sequential fault variant used')


def _events(log):
    found = []
    pattern = re.compile(r'^\[q4t\]\[t4_fault_(begin|reset|injection|drain|'
                         r'verify_return|return|end)\] (.*)$')
    for index, line in enumerate(log.splitlines()):
        match = pattern.fullmatch(line)
        if not match:
            require(not line.startswith('[q4t][t4_fault_') or line == VARIANT,
                    'unknown/malformed T4 observer event')
            continue
        pairs = [item.split('=', 1) for item in match[2].split()]
        require(all(len(pair) == 2 and all(pair) for pair in pairs),
                'malformed T4 observer fields')
        row = dict(pairs)
        require(len(row) == len(pairs), 'duplicate T4 observer field')
        require(re.fullmatch(r'[1-9]', row.get('ordinal', '')),
                'invalid T4 observer ordinal')
        found.append((match[1], row, index))
    return found


def validate_log(log):
    require_variant(log)
    paths = terminal_paths(log)
    require(list(paths) == [label for label, _ in PLAN],
            'expected exactly nine serial response paths')
    events = _events(log)
    require([int(row['ordinal']) for _, row, _ in events] ==
            sorted(int(row['ordinal']) for _, row, _ in events),
            'request lifetimes overlap')
    groups = []
    for ordinal, (label, kind) in enumerate(PLAN, 1):
        rows = [(event, row, line) for event, row, line in events
                if row['ordinal'] == str(ordinal)]
        expected = ['begin', 'reset', 'end']
        if kind != 'success':
            expected = ['begin', 'reset', 'injection', 'drain']
            if ordinal == 2:
                expected += ['verify_return', 'drain']
            expected += ['return', 'end']
        require([event for event, _, _ in rows] == expected,
                'missing/extra/unordered observer events: ' + label)
        require(all(row.get('real_ok') == '1' for _, row, _ in rows),
                'wrapped real operation did not succeed')
        begin, end = rows[0][1], rows[-1][1]
        for row, stage in ((begin, 'prefill'), (end, 'idle')):
            require(all(row.get(key) == value for key, value in {
                'stage': stage, 'position': '0', 'history': '0',
                'pending': '0'}.items()), 'host state not cleared')
        require(begin.get('slot') == rows[1][1].get('slot') == '0' and
                rows[1][1].get('completion') == 'stream_ordered',
                'wrong slot or draft reset scope')
        path_lines = [index for index, line in enumerate(log.splitlines())
                      if line.startswith('[q4t][decode_path] id=' + label + ' ')]
        require(len(path_lines) == 1 and
                rows[-2][2] < path_lines[0] < rows[-1][2],
                'response path not inside observed ownership lifetime')
        path = paths[label]
        require(path.get('requested_mtp') == '1' and
                path.get('verifier') == 't4' and path.get('fallback') == 'none',
                'wrong actual verifier or fallback')
        for field in ('steps', 'verifies', 'restores', 'extends', 'ordinary_calls'):
            require(re.fullmatch(r'[0-9]+', end.get(field, '')),
                    'invalid observer counter: ' + field)
        require(re.fullmatch(r'[0-9]+', path.get('mtp_steps', '')),
                'invalid completed step count')
        require(int(end['steps']) >= 1 and int(end['verifies']) >= 1,
                'no actual imported T4 step/verify observed')
        if kind == 'success':
            require(end.get('injected') == end.get('outputs_checked') ==
                    end.get('failed_before_end') == '0', 'success was injected')
            require(path.get('path') == 'mtp_multi_b1' and
                    int(path['mtp_steps']) == int(end['steps']) and
                    end['verifies'] == end['steps'] and
                    0 <= int(end['extends']) <= int(end['steps']),
                    'success was not real T4')
            # The combined terminal patch permits an ordinary output tail and
            # an EOS step without extend. Exact 256-token coverage is checked
            # from the HTTP response, not weakened here to favor early stops.
        else:
            by_kind = {event: row for event, row, _ in rows}
            injection, returned = by_kind['injection'], by_kind['return']
            require(injection.get('kind') == kind and
                    injection.get('pending') == '0' and
                    re.fullmatch(r'[0-9]+', injection.get('position', '')) and
                    injection.get('position') == injection.get('history'),
                    'fault identity or committed cursor mismatch')
            drains = [row for event, row, _ in rows if event == 'drain']
            require([row.get('scope') for row in drains] ==
                    (['verify', 'step'] if ordinal == 2 else ['step']),
                    'missing inner/outer checked completion')
            require(all(row.get('after_injection') == row.get('stream_match') ==
                        row.get('thread_match') == '1' for row in drains),
                    'drain does not follow injection on owning stream/thread')
            require(all(returned.get(key) == value for key, value in {
                'status': 'failed', 'counts': '0', 'next_b': '-1',
                'next_d0': '-1', 'caller_unchanged': '1', 'pending': '0',
                'observer_cuda_calls': '0', 'outer_drains': '1',
                'inner_drains': '1' if ordinal == 2 else '0',
                'next_g': 'invalid_not_read'}.items()),
                'failed step return contract differs')
            if ordinal == 2:
                require(all(by_kind['verify_return'].get(key) == value
                            for key, value in {
                    'status': 'failed', 'inner_drained': '1',
                    'checkpoint_rows': '0', 'checkpoint_slots': '0',
                    'observer_cuda_calls': '0'}.items()),
                    'verify returned before its own completion/invalidation')
            require(end.get('injected') == end.get('outputs_checked') ==
                    end.get('failed_before_end') == '1' and
                    end['ordinary_calls'] == '0' and
                    path.get('plain_tail_tokens') == '0',
                    'failure escaped to decode or normal state')
            completed = int(path['mtp_steps'])
            require(int(end['steps']) == completed + 1 and
                    path.get('path') == ('mtp_multi_b1' if completed else 'plain'),
                    'failed attempt published a completed step')
            if ordinal == 6:
                require(end['restores'] == '1', 'natural restore coverage absent')
            if ordinal == 8:
                require(end['extends'] == '1', 'first extend coverage absent')
        groups.append({'ordinal': ordinal, 'response_id': label,
                       'events': [row for _, row, _ in rows], 'path': path})
    failures = re.findall(r'^\[q4t\] generation failed id=(\S+)$', log,
                          re.MULTILINE)
    require(failures == [label for label, kind in PLAN if kind != 'success'],
            'unexpected/missing HTTP generation failures')
    control = {key: value for key, value in paths[PLAN[0][0]].items()
               if key != 'id'}
    for index in (2, 4, 6, 8):
        actual = {key: value for key, value in paths[PLAN[index][0]].items()
                  if key != 'id'}
        require(actual == control, 'T4 recovery path differs from fresh control')
    return {'passed': True, 'requests': 9, 'groups': groups,
            'scope': 'logical failure ownership/recovery; not numerical '
                     'T4 equivalence, performance or fatal CUDA recovery'}
