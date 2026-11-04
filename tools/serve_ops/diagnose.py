"""Collect a bounded read-only snapshot; never replay, cancel or restart work."""
import argparse
import datetime
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_ops import bundle, contracts, identity, privacy, transport
from tools.serve_ops.contracts import InvalidObservation, METRIC_PROFILES


def compare(previous, current):
    result = dict(state='unavailable', reason='no_previous_bundle', counters={})
    if previous is None:
        return result
    try:
        if (previous.get('version') != contracts.VERSION or
                previous['identity']['state'] != 'verified' or
                current['identity']['state'] != 'verified' or
                previous['identity']['process'] != current['identity']['process'] or
                previous['identity'].get('operations_process') != current['identity'].get('operations_process') or
                previous['port'] != current['port'] or
                previous.get('operations_port', 0) != current.get('operations_port', 0) or
                previous.get('observability_version', 3) != current.get('observability_version', 3) or
                previous['configuration']['protocol_sha256'] != current['configuration']['protocol_sha256']):
            result['reason'] = 'instance_or_contract_changed'
            return result
        elapsed = current['monotonic']-previous['monotonic']
        if not math.isfinite(elapsed) or not 0 < elapsed <= 3600:
            raise InvalidObservation('interval_out_of_range')
        values = {}
        for name, kind in METRIC_PROFILES[current.get('observability_version', 3)].items():
            if kind != 'counter':
                continue
            old = contracts.integer(previous['metrics'][name])
            new = contracts.integer(current['metrics'][name])
            if new < old:
                raise InvalidObservation('counter_decreased')
            values[name] = new-old
        return dict(state='available', seconds=elapsed, counters=values,
                    scope='same verified process only; per-counter, non-atomic; not additive outcomes')
    except InvalidObservation as error:
        result['reason'] = error.code
    except (KeyError, TypeError, ValueError):
        result['reason'] = 'invalid_previous_snapshot'
    return result


def findings(report):
    result = []
    def add(code, severity, evidence):
        result.append(dict(code=code, severity=severity, evidence=evidence,
            action='consult_runbook', automatic_retry=False, automatic_restart=False))
    if report['identity']['state'] != 'verified':
        add('identity_unverified', 'unknown', report['identity']['state'])
    for path, observation in report['probes'].items():
        if observation.get('error'):
            add('endpoint_observation_failed', 'unknown', dict(route=path, error=observation['error']))
    metrics = report.get('metrics')
    if not metrics:
        return result
    if not metrics['q4t_gpu_healthy']:
        add('gpu_unhealthy', 'critical', 'q4t_gpu_healthy')
    if metrics['q4t_memory_admission_enabled']:
        if not metrics['q4t_memory_observation_valid']:
            add('memory_observation_unavailable', 'critical', 'q4t_memory_observation_valid')
        elif not metrics['q4t_memory_admission_open']:
            add('memory_pressure', 'warning', 'q4t_memory_admission_open')
    else:
        add('memory_admission_disabled', 'information', 'explicit configuration')
    if not metrics['q4t_ready']:
        add('not_ready', 'warning', 'q4t_ready')
    proc = report['identity'].get('process', {})
    if proc.get('listener_scope') == 'wildcard':
        add('wildcard_backend_listener', 'warning', 'local listener ownership; ingress not audited')
    if report.get('log', {}).get('error'):
        add('log_projection_unavailable', 'unknown', report['log']['error'])
    interval = report['comparison']
    if interval['state'] == 'available':
        changed = interval['counters']
        selectors = {
            'admission_rejections_observed': ['q4t_queue_rejected_total',
                'q4t_text_admission_rejected_total', 'q4t_structured_rejected_total'] +
                [n for n in changed if n.startswith(('q4t_memory_rejected_total{',
                                                     'q4t_connection_rejections_total{',
                                                     'q4t_operations_connection_rejections_total{'))],
            'structured_failures_observed': ['q4t_structured_prepare_failed_total',
                'q4t_structured_compile_timeouts_total', 'q4t_structured_resource_exhausted_total'],
            'chat_errors_observed': ['q4t_requests_error_total'],
        }
        for code, names in selectors.items():
            evidence = {name:changed[name] for name in names if changed[name] > 0}
            if evidence:
                add(code, 'information' if code=='chat_errors_observed' else 'warning', evidence)
    return result


def snapshot(port, pid, release_path, *, log_path=None, previous_path=None,
             operations_port=0, client_policy_path=None):
    contracts.integer(operations_port, 0, 65535)
    if operations_port == port:
        raise InvalidObservation('operations_listener_port_conflict')
    started, wall = time.monotonic(), time.time()
    report = dict(version=contracts.VERSION, monotonic=started, unix_seconds=wall,
        collected_at=datetime.datetime.fromtimestamp(wall, datetime.timezone.utc).isoformat(),
        port=port, operations_port=operations_port, probes={}, metrics=None,
        configuration=None, identity=dict(state='unavailable'),
        observability_version=None,
        log=dict(state='not_requested', raw_log_included=False),
        read_only=True, automatic_retry=False, automatic_restart=False,
        snapshot_atomic=False, physical_ram_hard_limit_proved=False)
    release = before = after = previous = ops_before = ops_after = None
    credentials = policy_binding = None
    policy_valid = True
    if client_policy_path is not None:
        from tools.serve_gateway import client_policy as cp
        try:
            raw, selected = cp.read(client_policy_path)
            credentials = dict(client=selected['clients'][0]['id'], token=selected['gateway_token'])
            policy_binding = cp.binding(raw)
        except (OSError, ValueError):
            policy_valid = False
            report['identity']['error'] = 'diagnostic_client_policy_unavailable'
    try:
        release = identity.release_identity(release_path)
        before = identity.process(pid, port)
        if operations_port:
            ops_before = identity.operations(before, operations_port)
    except InvalidObservation as error:
        report['identity']['error'] = error.code
    protocol = None
    for path in transport.ROUTES:
        probe = {}
        try:
            options = dict(deadline=started+10)
            if path == '/v1/capabilities' and credentials is not None:
                # Never disclose the internal gateway credential to a socket
                # whose process/release ownership has already failed validation.
                if (not policy_valid or not release or not before or
                        before['executable_sha256'] != release['binary_sha256'] or
                        operations_port and ops_before is None):
                    raise InvalidObservation('diagnostic_credential_owner_unverified')
                if (not identity.same_instance(before, identity.process(pid, port)) or
                        operations_port and not identity.same_instance(
                            ops_before, identity.operations(before, operations_port))):
                    raise InvalidObservation('diagnostic_credential_owner_changed')
                options['credentials'] = credentials
            value = transport.get(operations_port or port, path, **options)
            probe['http_status'] = value['http_status']
            media = 'text/plain' if path=='/metrics' else 'application/json'
            if value['content_type'] != media:
                raise InvalidObservation('unexpected_content_type')
            if path == '/metrics':
                if value['http_status'] != 200:
                    raise InvalidObservation('metrics_unavailable')
                report['metrics'], probe['omitted_series'] = contracts.parse_metrics(
                    value['body'], version=report['observability_version'] or 3)
            else:
                document = contracts.json_object(value['body'])
                if path in ('/readyz', '/livez'):
                    probe['readiness'] = contracts.readiness(document, value['http_status'],
                                                             liveness=path=='/livez')
                elif value['http_status'] != 200:
                    raise InvalidObservation('contract_endpoint_unavailable')
                elif path == '/v1/config':
                    report['configuration'] = contracts.configuration(
                        document, version=report['observability_version'] or 3)
                    if policy_binding is not None:
                        from tools.serve_gateway import client_policy as cp
                        try:
                            cp.observed_matches(document.get('client_policy'), policy_binding)
                        except ValueError:
                            policy_valid = False
                            raise InvalidObservation('diagnostic_client_policy_mismatch') from None
                else:
                    candidate = document.get('protocol_source_sha256')
                    observation = document.get('observability')
                    if (not isinstance(candidate, str) or not contracts.HEX.fullmatch(candidate) or
                            document.get('object') != 'q4t.capabilities' or
                            not isinstance(observation, dict) or
                            type(observation.get('version')) is not int or
                            observation['version'] not in METRIC_PROFILES):
                        raise InvalidObservation('capabilities_contract_unavailable')
                    if observation['version'] in (4, 5):
                        http = document.get('http')
                        if (not isinstance(http, dict) or
                                http.get('connection_profile') != ('q4t-http-partitions-v2'
                                    if observation['version'] == 5 else 'q4t-http-partitions-v1') or
                                http.get('observation_reserved_connections') != 4 or
                                http.get('control_reserved_connections') != 4 or
                                http.get('observation_routes') != ['GET /metrics', 'GET /v1/config',
                                    'GET /v1/capabilities', 'GET /v1/models'] or
                                observation.get('business_connections_exclude_observation') is not True):
                            raise InvalidObservation('capabilities_partition_unavailable')
                        if observation['version'] == 5:
                            ops = http.get('operations_listener')
                            if (not isinstance(ops, dict) or
                                    any(ops.get(k) != v for k, v in dict(
                                        enabled_by='--operations-port', host='127.0.0.1',
                                        handlers=8, control_connections=4,
                                        observation_connections=4,
                                        client_authentication='unchanged').items()) or
                                    ops.get('generation') is not False or
                                    ops.get('preflight') is not False or
                                    ops.get('public_capacity_independent') is not True):
                                raise InvalidObservation('capabilities_operations_unavailable')
                    report['observability_version'] = observation['version']
                    protocol = candidate
        except InvalidObservation as error:
            probe['error'] = error.code
        report['probes'][path] = probe
    try:
        after = identity.process(pid, port)
        if operations_port:
            ops_after = identity.operations(after, operations_port)
    except InvalidObservation as error:
        report['identity']['error'] = error.code
    if (identity.same_instance(before, after) and release and report['configuration'] and
            (not operations_port or identity.same_instance(ops_before, ops_after))):
        compatible = (before['executable_sha256'] == release['binary_sha256'] and
            protocol == release['protocol_sha256'] == report['configuration']['protocol_sha256'] and
            policy_valid and (report['observability_version'] != 5 and not operations_port or
                report['observability_version'] == 5 and
                report['configuration'].get('public_port') == port and
                report['configuration'].get('operations_port') == operations_port))
        report['identity'] = dict(state='verified' if compatible else 'mismatch',
            process=before, expected=release, capability_protocol=protocol)
        if operations_port:
            report['identity']['operations_process'] = ops_before
    if log_path is not None:
        try:
            report['log'] = privacy.read_log(log_path)
        except InvalidObservation as error:
            report['log'] = dict(state='unavailable', error=error.code, raw_log_included=False)
    if previous_path is not None:
        try:
            previous = bundle.read(previous_path)
        except InvalidObservation:
            previous = {}
    report['comparison'] = compare(previous, report)
    report['findings'] = findings(report)
    severities = {item['severity'] for item in report['findings']}
    report['state'] = ('UNKNOWN' if 'unknown' in severities else 'CRITICAL' if 'critical' in severities
                       else 'WARNING' if 'warning' in severities else 'HEALTHY')
    report['duration_seconds'] = time.monotonic()-started
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--operations-port', type=int, default=0)
    parser.add_argument('--client-policy-file', type=Path)
    parser.add_argument('--release', type=Path, default=ROOT/'build/q4t.release.json')
    parser.add_argument('--log', type=Path)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        port = transport.endpoint(args.base_url)
        report = snapshot(port, args.pid, args.release, log_path=args.log, previous_path=args.previous,
                          operations_port=args.operations_port, client_policy_path=args.client_policy_file)
        bundle.write(args.output, report)
        print(report['state'])
        return {'HEALTHY':0, 'WARNING':1, 'CRITICAL':2, 'UNKNOWN':3}[report['state']]
    except (InvalidObservation, OSError, ValueError):
        # Argparse describes syntax; peer/local content and secret paths stay out.
        print('diagnostic collection or private bundle creation failed', file=sys.stderr)
        return 4


if __name__ == '__main__':
    sys.exit(main())
