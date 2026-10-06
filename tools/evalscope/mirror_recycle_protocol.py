"""Bounded mirror-recycle configuration and per-request counter contracts."""
import re

PREFIX = '[q4t][mirror_gpu_recycle]'
COUNTERS = ('plans', 'attempts', 'preferred', 'changed', 'fallback',
            'unavailable', 'published')
SCHEMA = 'q4t.mirror_gpu_recycle.v1'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def mirror_recycle_evidence(log, requests, enabled):
    """Validate one startup and one ordered summary per completed HTTP request.

    Counters describe real selection work, not counterfactual saved reads or
    speed. Explicit-phase correctness additionally needs the C++ scope contract;
    this parser can only bind the declared scope and observed request deltas.
    """
    require(type(enabled) is int and enabled in (0, 1), 'invalid recycle bit')
    require(isinstance(requests, list) and requests, 'no HTTP requests to audit')
    lines = [line for line in log.splitlines() if PREFIX in line]
    startup = re.compile(re.escape(PREFIX) +
        r' enabled=([01]) scope=explicit_single_decode schema=' +
        re.escape(SCHEMA) + r'\s*$')
    require(lines, 'missing mirror recycle configuration')
    match = startup.search(lines[0])
    require(match is not None and int(match[1]) == enabled,
            'mirror recycle startup configuration differs')
    pattern = re.compile(re.escape(PREFIX) + r' id=(\S+)' +
        ''.join(r' ' + key + r'=(\d+)' for key in COUNTERS) + r'\s*$')
    require(len(lines) == len(requests) + 1,
            'mirror recycle request summaries missing or duplicated')
    seen = set()
    rows = []
    total = dict.fromkeys(COUNTERS, 0)
    for line, request in zip(lines[1:], requests):
        match = pattern.search(line)
        require(match is not None, 'malformed mirror recycle request summary')
        request_id = request.get('response_id')
        output = request.get('actual_output')
        require(request.get('response_id_valid') is True and
                isinstance(request_id, str) and request_id and
                request_id not in seen and match[1] == request_id,
                'mirror recycle HTTP identity or order differs')
        require(type(output) is int and output >= 1,
                'mirror recycle output count invalid')
        seen.add(request_id)
        row = dict(zip(COUNTERS, map(int, match.groups()[1:])))
        require(all(0 <= value < 2**64 for value in row.values()),
                'mirror recycle counter outside uint64 range')
        require(row['attempts'] == row['preferred'] + row['fallback'] +
                row['unavailable'], 'mirror recycle attempts do not close')
        require(row['changed'] <= row['preferred'] and
                row['published'] <= row['preferred'],
                'mirror recycle subset counters exceed preferred')
        require(row['plans'] <= 48 * (output - 1),
                'mirror recycle plans exceed true decode layer count')
        require(row['plans'] > 0 or not any(row.values()),
                'mirror recycle selection without an active plan')
        require(enabled == 1 or not any(row.values()),
                'disabled mirror recycle reports active work')
        for key in COUNTERS:
            total[key] += row[key]
        rows.append({'response_id': request_id, **row})
    return {'schema': SCHEMA, 'enabled': enabled, 'passed': True,
            'request_count': len(rows), 'requests': rows, 'totals': total,
            'selection_change_observed': total['changed'] > 0,
            'declared_scope': 'explicit_single_decode',
            'runtime_phase_gate_proven_by_parser': False,
            'numerical_acceptance': False, 'performance_acceptance': False,
            'read_savings_or_speed_inferred_from_counters': False}
