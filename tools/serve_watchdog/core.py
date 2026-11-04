"""Receiver-side dead-man state; no network, credentials or model control."""
import calendar
import copy
import datetime
import hashlib
import json
import math
import re

TIMEOUT = 180
HISTORY = 64
STATES = ('WAITING', 'PRESENT', 'MISSING', 'RESOLVED', 'UNKNOWN')
MAXIMUM = 64 * 1024
COUNTER_MAX = 2**63 - 1


class ContractError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise ContractError(message)


def parse(raw):
    require(isinstance(raw, bytes) and len(raw) <= MAXIMUM, 'input_size')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate_key')
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: require(False, 'nonfinite_json'))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ContractError('invalid_json') from error


def number(value):
    return (type(value) in (float, int) and 0 <= value < 2**53
            and math.isfinite(value))


def clock(value):
    require(isinstance(value, dict) and set(value) ==
            {'boot_id', 'boottime', 'unix_seconds'}, 'clock_fields')
    require(isinstance(value['boot_id'], str) and re.fullmatch(
        r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', value['boot_id']),
        'boot_id')
    require(number(value['boottime']) and number(value['unix_seconds']),
            'clock_value')
    return value


def timestamp(value):
    """RFC3339 with up to nanoseconds, preserving incident ordering."""
    require(isinstance(value, str) and len(value) <= 40, 'timestamp_type')
    match = re.fullmatch(
        r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})'
        r'(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})', value)
    require(match is not None, 'timestamp_format')
    try:
        date = datetime.datetime.fromisoformat(match[1] + match[3])
        seconds = calendar.timegm(date.utctimetuple())
    except (ValueError, OverflowError) as error:
        raise ContractError('timestamp_value') from error
    return seconds * 10**9 + int((match[2] or '').ljust(9, '0'))


def heartbeat(raw, deployment):
    value = parse(raw)
    require(isinstance(value, dict), 'envelope_type')
    require(value.get('version') == '4' and
            value.get('receiver') == 'q4t-webhook' and
            value.get('status') in ('firing', 'resolved') and
            type(value.get('truncatedAlerts')) is int and
            value['truncatedAlerts'] == 0, 'envelope_identity')
    alerts = value.get('alerts')
    require(isinstance(alerts, list) and len(alerts) == 1, 'heartbeat_only')
    alert = alerts[0]
    require(isinstance(alert, dict) and
            alert.get('labels') == dict(alertname='Q4TNotifyHeartbeat',
                deployment=deployment, component='q4t-monitor',
                severity='information'), 'heartbeat_labels')
    require(alert.get('status') == value['status'], 'heartbeat_status')
    require(isinstance(alert.get('fingerprint'), str) and re.fullmatch(
        r'[0-9a-f]{16}', alert['fingerprint']), 'fingerprint')
    start = timestamp(alert.get('startsAt'))
    end = timestamp(alert.get('endsAt'))
    require(start > 0 and (end >= start or
        (alert['status'] == 'firing' and
         alert['endsAt'] == '0001-01-01T00:00:00Z')), 'incident_times')
    return dict(status=alert['status'], starts_at=alert['startsAt'],
        fingerprint=alert['fingerprint'],
        payload_sha256=hashlib.sha256(raw).hexdigest())


def new_state(policy_sha256, now):
    clock(now)
    return dict(version=1, policy_sha256=policy_sha256, created=now.copy(),
        receipt=None, accepted=0, ignored=0, transitions=0, history=[])


def validate(state, digest):
    require(isinstance(state, dict) and set(state) == {
        'version', 'policy_sha256', 'created', 'receipt', 'accepted',
        'ignored', 'transitions', 'history'}, 'state_fields')
    require(type(state['version']) is int and state['version'] == 1 and
            state['policy_sha256'] == digest, 'state_binding')
    clock(state['created'])
    for key in ('accepted', 'ignored', 'transitions'):
        require(type(state[key]) is int and
                0 <= state[key] <= COUNTER_MAX, 'state_counter')
    receipt = state['receipt']
    if receipt is not None:
        require(isinstance(receipt, dict) and set(receipt) == {
            'status', 'starts_at', 'fingerprint', 'payload_sha256',
            'received'}, 'receipt_fields')
        require(receipt['status'] in ('firing', 'resolved') and
            isinstance(receipt['fingerprint'], str) and
            re.fullmatch(r'[0-9a-f]{16}', receipt['fingerprint']) and
            isinstance(receipt['payload_sha256'], str) and
            re.fullmatch(r'[0-9a-f]{64}', receipt['payload_sha256']),
            'receipt_identity')
        require(timestamp(receipt['starts_at']) > 0, 'receipt_time')
        clock(receipt['received'])
    require((receipt is None) == (state['accepted'] == 0), 'receipt_count')
    history = state['history']
    require(isinstance(history, list) and
        len(history) == min(HISTORY, state['transitions']), 'history_count')
    for index, event in enumerate(history):
        require(isinstance(event, dict) and set(event) ==
            {'sequence', 'state', 'reason', 'observed'}, 'event_fields')
        require(type(event['sequence']) is int and event['sequence'] ==
            state['transitions'] - len(history) + index + 1 and
            event['state'] in STATES and event['reason'] in {
                'initial_grace', 'no_heartbeat', 'heartbeat_received',
                'heartbeat_expired', 'heartbeat_resolved',
                'receiver_rebooted', 'clock_regressed'}, 'event_identity')
        clock(event['observed'])
    return state


def status(state, now):
    clock(now)
    receipt = state['receipt']
    origin = receipt['received'] if receipt else state['created']
    age = None
    if now['boot_id'] != origin['boot_id']:
        name, reason = 'UNKNOWN', 'receiver_rebooted'
    elif now['boottime'] < origin['boottime']:
        name, reason = 'UNKNOWN', 'clock_regressed'
    else:
        age = now['boottime'] - origin['boottime']
        if receipt and receipt['status'] == 'resolved':
            name, reason = 'RESOLVED', 'heartbeat_resolved'
        elif age >= TIMEOUT:
            name = 'MISSING'
            reason = 'heartbeat_expired' if receipt else 'no_heartbeat'
        elif receipt:
            name, reason = 'PRESENT', 'heartbeat_received'
        else:
            name, reason = 'WAITING', 'initial_grace'
    return dict(state=name, reason=reason, healthy=name == 'PRESENT',
        age_seconds=age, timeout_seconds=TIMEOUT, observed=now.copy(),
        accepted=state['accepted'], ignored=state['ignored'],
        transitions=state['transitions'],
        history_dropped=max(0, state['transitions'] - HISTORY),
        last_received_unix_seconds=(receipt['received']['unix_seconds']
                                    if receipt else None))


def increment(state, name):
    require(state[name] < COUNTER_MAX, 'counter_exhausted')
    state[name] += 1


def observe(state, now):
    result = copy.deepcopy(state)
    value = status(result, now)
    previous = result['history'][-1] if result['history'] else {}
    if (value['state'], value['reason']) != (
            previous.get('state'), previous.get('reason')):
        increment(result, 'transitions')
        result['history'].append(dict(sequence=result['transitions'],
            state=value['state'], reason=value['reason'], observed=now.copy()))
        result['history'] = result['history'][-HISTORY:]
    return result


def receive(state, alert, now):
    # Account for an outage even if the periodic observer was stopped.
    result = observe(state, now)
    prior = result['receipt']
    c1, c2 = (prior['received'] if prior else result['created']), clock(now)
    require(c1['boot_id'] != c2['boot_id'] or
            c1['boottime'] <= c2['boottime'], 'clock_regressed')
    if prior:
        start, previous = timestamp(alert['starts_at']), timestamp(
            prior['starts_at'])
        if start < previous:
            increment(result, 'ignored')
            return result, 'ignored_stale_incident'
    # Standard AM repeat deliveries can be byte-identical. Deduplicating them
    # would report a healthy pipeline dead. Arrival time, not startsAt/endsAt,
    # is the liveness evidence. Authenticated replay or same-incident out-of-
    # order deliveries cannot be distinguished, including after a resolution.
    result['receipt'] = dict(alert, received=clock(now).copy())
    increment(result, 'accepted')
    return observe(result, now), 'accepted'


def metrics(value, deployment):
    require(re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', deployment), 'deployment')
    label = 'deployment="' + deployment + '"'
    rows = ['# HELP q4t_watchdog_state Current receiver state, one-hot.',
            '# TYPE q4t_watchdog_state gauge']
    rows += [f'q4t_watchdog_state{{{label},state="{name}"}} '
             f'{int(value["state"] == name)}' for name in STATES]
    for name, number in (
        ('evaluated_timestamp_seconds', value['observed']['unix_seconds']),
        ('last_received_timestamp_seconds',
         value['last_received_unix_seconds'] or 0),
        ('heartbeat_age_seconds', value['age_seconds']
         if value['age_seconds'] is not None else -1),
        ('accepted_total', value['accepted']),
        ('ignored_total', value['ignored']),
        ('transitions_total', value['transitions']),
        ('history_dropped_total', value['history_dropped'])):
        kind = 'counter' if name.endswith('_total') else 'gauge'
        rows += [f'# HELP q4t_watchdog_{name} {name.replace("_", " ")}.',
                 f'# TYPE q4t_watchdog_{name} {kind}',
                 f'q4t_watchdog_{name}{{{label}}} {number}']
    return '\n'.join(rows) + '\n'
