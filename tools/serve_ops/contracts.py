"""Fixed operational projection of the current serve metric contract."""
from decimal import Decimal, InvalidOperation
import json
import math
import re

VERSION = 'q4t.ops.snapshot.v1'
STATES = ('disabled', 'open', 'memory_pressure', 'memory_observation_unavailable')
ABORTS = ('client_disconnected', 'explicit_cancel', 'deadline_exceeded',
          'server_shutdown', 'request_failed', 'other')
RECEIVE = ('malformed', 'header_too_large', 'body_too_large', 'body_capacity',
           'header_deadline', 'body_deadline', 'header_eof', 'body_eof',
           'socket_error', 'admission', 'connection_capacity', 'server_draining',
           'unsupported_media', 'method', 'query', 'unexpected_body', 'route', 'exception')
WRITE = ('write_deadline', 'request_deadline', 'explicit_cancel', 'peer_closed',
         'server_shutdown', 'request_failure', 'socket_error', 'exception')
GAUGES = ('memory_admission_enabled', 'memory_observation_valid',
          'memory_available_bytes', 'memory_admission_floor_bytes',
          'memory_admission_recovery_bytes', 'memory_admission_open',
          'structured_workers_active', 'active_chats', 'queued_requests',
          'request_body_bytes', 'business_connections', 'control_connections',
          'ready', 'gpu_healthy', 'structured_active', 'structured_preparing',
          'structured_vocab_bytes', 'seq_slots_total', 'seq_slots_free')
COUNTERS = ('memory_observations_total', 'memory_observation_failures_total',
            'memory_admission_transitions_total', 'requests_total',
            'requests_success_total', 'requests_error_total', 'requests_aborted_total',
            'structured_rejected_total', 'structured_prepare_failed_total',
            'structured_workers_started_total', 'structured_workers_reaped_total',
            'structured_compile_timeouts_total', 'structured_resource_exhausted_total',
            'queue_rejected_total', 'text_admission_rejected_total',
            'execution_steps_total', 'prompt_tokens_total', 'generation_tokens_total')
BOOLEAN = {'q4t_'+s for s in ('memory_admission_enabled', 'memory_observation_valid',
                            'memory_admission_open', 'ready', 'gpu_healthy')}


class InvalidObservation(ValueError):
    """Fixed code only: never format untrusted input into an exported error."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def key(name, **labels):
    return name + ('{' + ','.join(f'{k}="{v}"' for k, v in sorted(labels.items())) + '}'
                   if labels else '')


def metric_contract():
    result = {'q4t_'+name:'gauge' for name in GAUGES}
    result.update({'q4t_'+name:'counter' for name in COUNTERS})
    for state in STATES:
        result[key('q4t_memory_admission_state', state=state)] = 'gauge'
    for point in ('headers', 'prepare', 'execute'):
        for reason in STATES[2:]:
            result[key('q4t_memory_rejected_total', checkpoint=point, reason=reason)] = 'counter'
    for name, values in (('q4t_requests_aborted_by_reason_total', ABORTS),
                         ('q4t_http_receive_failures_total', RECEIVE)):
        for reason in values:
            result[key(name, reason=reason)] = 'counter'
    for origin in ('handler', 'heartbeat'):
        for reason in WRITE:
            result[key('q4t_http_write_failures_total', origin=origin, reason=reason)] = 'counter'
    return result


METRICS = metric_contract()
FAMILIES = {name.split('{', 1)[0] for name in METRICS}
METRICS_V4 = dict(METRICS, q4t_observation_connections='gauge')
for _kind in ('unclassified', 'business', 'observation', 'control'):
    METRICS_V4[key('q4t_connection_rejections_total', kind=_kind)] = 'counter'
METRIC_PROFILES = {3: METRICS, 4: METRICS_V4}
HEX = re.compile(r'[0-9a-f]{64}\Z')


def integer(value, minimum=0, maximum=2**64-1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidObservation('invalid_integer')
    return value


def json_object(raw):
    # CPython JSON parser depth behavior varies; bound nesting before parsing.
    try:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        if not isinstance(raw, str) or len(raw) > 2*1024**2:
            raise InvalidObservation('json_size_limit')
        depth, quoted, escaped = 0, False, False
        for char in raw:
            if quoted:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in '[{':
                depth += 1
                if depth > 64:
                    raise InvalidObservation('json_depth_limit')
            elif char in ']}':
                depth -= 1
    except UnicodeError:
        raise InvalidObservation('invalid_json') from None
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise InvalidObservation('duplicate_json_key')
            result[name] = value
        return result
    def constant(_):
        raise InvalidObservation('nonfinite_json')
    def floating(raw_number):
        value = float(raw_number)
        if not math.isfinite(value):
            raise InvalidObservation('nonfinite_json')
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant,
                           parse_float=floating)
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidObservation('invalid_json') from None
    if not isinstance(value, dict):
        raise InvalidObservation('object_required')
    return value


def parse_metrics(raw, *, version=3):
    """Only fixed metric names/labels leave this function; no unknown text."""
    if len(raw) > 262144 or not raw.endswith('\n'):
        raise InvalidObservation('incomplete_metrics')
    if type(version) is not int or version not in METRIC_PROFILES:
        raise InvalidObservation('unsupported_observation_version')
    contract = METRIC_PROFILES[version]
    families = {name.split('{', 1)[0] for name in contract}
    result, omitted = {}, 0
    for line in raw.splitlines():
        if not line or line.startswith('#'):
            continue
        family = re.match(r'[a-zA-Z_:][a-zA-Z0-9_:]*', line)
        if not family or family[0] not in families:
            omitted += 1
            continue
        match = re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^{}]*)\})?\s+(\S+)', line)
        if not match:
            raise InvalidObservation('invalid_metric_syntax')
        labels = {}
        if match[2]:
            for entry in match[2].split(','):
                label = re.fullmatch(r'([a-zA-Z_][a-zA-Z0-9_]*)="([a-zA-Z0-9_]+)"', entry)
                if not label or label[1] in labels:
                    raise InvalidObservation('invalid_metric_labels')
                labels[label[1]] = label[2]
        name = key(match[1], **labels)
        if name not in contract or name in result:
            raise InvalidObservation('unexpected_or_duplicate_metric')
        try:
            value = Decimal(match[3])
            if not value.is_finite() or value != value.to_integral_value() or not 0 <= value < 2**64:
                raise InvalidObservation('invalid_metric_value')
            result[name] = int(value)
        except InvalidOperation:
            raise InvalidObservation('invalid_metric_value') from None
    if set(result) != set(contract):
        raise InvalidObservation('missing_required_metric')
    if any(result[name] not in (0, 1) for name in BOOLEAN):
        raise InvalidObservation('invalid_boolean_metric')
    if result['q4t_seq_slots_free'] > result['q4t_seq_slots_total']:
        raise InvalidObservation('invalid_slot_observation')
    if version == 4 and result['q4t_observation_connections'] > 4:
        raise InvalidObservation('invalid_observation_owners')
    states = [result[key('q4t_memory_admission_state', state=s)] for s in STATES]
    if any(n not in (0, 1) for n in states) or sum(states) != 1:
        raise InvalidObservation('invalid_memory_state')
    enabled, valid, opened = [result['q4t_'+n] for n in (
        'memory_admission_enabled', 'memory_observation_valid', 'memory_admission_open')]
    if (states[0] != (not enabled) or states[1] != (enabled and opened) or
            states[3] != (enabled and not valid) or
            (not enabled and not opened) or (not valid and result['q4t_memory_available_bytes'] != 0)):
        raise InvalidObservation('inconsistent_memory_state')
    return result, omitted


def readiness(value, http_status, *, liveness=False):
    if set(value) != {'status', 'live', 'ready'} or value['status'] not in (
            'ready', 'starting', 'draining', 'unhealthy', *STATES[2:]):
        raise InvalidObservation('invalid_readiness')
    if type(value['live']) is not bool or type(value['ready']) is not bool:
        raise InvalidObservation('invalid_readiness')
    if (value['ready'] and not value['live']) or value['ready'] != (value['status']=='ready'):
        raise InvalidObservation('invalid_readiness')
    if http_status != (200 if value['live' if liveness else 'ready'] else 503):
        raise InvalidObservation('readiness_status_mismatch')
    return value


def configuration(value, *, version=3):
    """No model name, paths, argv, credentials or arbitrary strings in bundle."""
    try:
        protocol = value['protocol_source_sha256']
        if value['profile'] != 'q4t-config-v1' or not HEX.fullmatch(protocol):
            raise InvalidObservation('invalid_config_profile')
        result = {'protocol_sha256':protocol}
        for name in ('max_context_tokens', 'max_sequences', 'prefill_chunk_tokens',
                     'request_deadline_ms', 'max_inflight_requests', 'max_queued_requests'):
            result[name] = integer(value[name]['effective'], maximum=2**31-1)
        mem = value['memory_admission']
        result['memory_floor_mib'] = integer(mem['min_available_mib']['effective'], maximum=1048576)
        result['memory_recovery_mib'] = integer(mem['recovery_threshold_mib'], maximum=2097152)
        if type(mem['enabled']) is not bool or mem['hard_limit'] is not False:
            raise InvalidObservation('invalid_memory_contract')
        result['memory_enabled'] = mem['enabled']
        result['whole_server_ram_limit'] = False
        for name in ('mtp_enabled', 'structured_backend'):
            value_bool = value['runtime'][name]
            if type(value_bool) is not bool:
                raise InvalidObservation('invalid_runtime_contract')
            result[name] = value_bool
        if version == 4:
            limits = value['limits']
            for name in ('business_connections', 'observation_connections',
                         'control_connections', 'connection_handlers'):
                result[name] = integer(limits[name], 1, 2**31-1)
            if (result['observation_connections'] != 4 or result['control_connections'] != 4 or
                    result['connection_handlers'] != result['business_connections']+8 or
                    limits['unclassified_shares_total'] is not True):
                raise InvalidObservation('invalid_connection_partitions')
        elif version != 3:
            raise InvalidObservation('unsupported_observation_version')
        return result
    except (KeyError, TypeError):
        raise InvalidObservation('missing_config_contract') from None
