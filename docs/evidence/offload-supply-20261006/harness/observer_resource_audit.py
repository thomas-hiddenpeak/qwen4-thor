"""Read resource evidence from exactly five terminal observer services.

Reuse immutable raw-counter/window arithmetic, not an earlier experiment's
admission or recovery entry. No model payload, route parsing, HTTP, subprocess,
GPU query, cache advice, or tests. Quality11 has one shared client I/O envelope.
"""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
import time

sys.dont_write_bytecode = True
R = Path(__file__).resolve().parent
O = R / 'observer-source'
EVIDENCE = O / '.q4t-work/evidence'
BASE = R.parent / 'offload-decode-log-20261005/audit_resources.py'
BASE_SHA = '10959f8021c7e2f6479ee77c8d163484054839a72fb172153bd723813f7ffde7'
CORE = R.parent / 'offload-partition-runtime-20261003/audit_raw_resources_frozen.py'
CORE_SHA = '035f1f840d00350a7b6db62e1b8ef7f62a5fda02406dae91395d291a67652da4'
RECEIPT = R.parent / 'offload-autonomous-20261003/raw-audit-execution-01/self-test-exit.json'
RECEIPT_SHA = '8fcf469238691ef2993e5c453ffee4df549efa25e15c210b0e5ff116b47f655f'
GROUPS = ('q01-quality-on', 's01-as-off', 's02-as-on', 's03-al-on', 's04-al-off')
SCOPE = 'offload_decode_supply_observer_v1'
RAW_NAMES = {'resource-samples.jsonl', 'memory.csv', 'endpoints.jsonl'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def load_checked(path, digest, name):
    require(hashlib.sha256(path.read_bytes()).hexdigest() == digest,
            'immutable resource dependency SHA differs: ' + str(path))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def identity(record, plan, expected):
    return (record.get('plan_sha256') == expected and
            all(record.get(key) == plan[key] for key in
                ('runtime_source_commit', 'runtime_binary_sha256')))


def terminal(meta, name, plan, expected, audit_started):
    """All five services must pass this gate before any raw stream is read."""
    directory = EVIDENCE / name
    start = meta.json(R / (name + '-controller-start.json'))
    exit_record = meta.json(R / (name + '-controller-exit.json'))
    controller = meta.json(R / (name + '-stage.json'))
    decision = meta.json(R / (name + '-decision.json'))
    quality = name == GROUPS[0]
    count = 11 if quality else 4
    require(identity(controller, plan, expected) and
            identity(decision, plan, expected) and
            controller['group_id'] == decision['group_id'] == name and
            controller['first_runtime_test'] is quality and
            controller['returncode'] == exit_record['returncode'] == 0 and
            controller['cleanup_complete'] is exit_record['cleanup_complete'] is True and
            controller['failure'] is exit_record['failure'] is None,
            name + ': controller identity, terminal or cleanup differs')
    require(all(finite(value) for value in (start['started_t'],
                exit_record['started_t'], exit_record['ended_t'],
                controller['started_t'], controller['ended_t'], decision['recorded_t']))
            and start['started_t'] == exit_record['started_t'] == controller['started_t']
            and controller['started_t'] < exit_record['ended_t'] <= controller['ended_t']
            == decision['ended_t'] <= decision['recorded_t'] <= audit_started,
            name + ': invalid completed owner time containment')
    require(decision['passed'] is True and decision['request_count'] == count and
            decision['performance_acceptance'] is False and decision['status'] ==
            ('PASS_FIXED_HTTP_11' if quality else 'PASS_GROUP_CONTRACTS'),
            name + ': successful fixed HTTP decision missing')
    wrapper = meta.json(directory / 'wrapper-exit.json')
    http = meta.json(directory / 'http/exit.json')
    runner = meta.json(directory / 'runner-process-group.json')
    cleanup = meta.json(directory / 'http/isolation/cleanup.json')
    protocol = meta.json(directory / 'protocol.json')
    require(wrapper['runner_rc'] == wrapper['monitor_rc'] == http['server'] == 0 and
            wrapper['failure'] is http['failure'] is http['cleanup_failure'] is None and
            wrapper['cleanup_failed'] is False and
            wrapper['unit_after_cleanup']['LoadState'] == 'not-found' and
            http['http_output_checks_passed'] is True and
            http['performance_acceptance'] is False and http['completed'] == count,
            name + ': wrapper/HTTP is incomplete')
    require(cleanup['stop_rc'] == 0 and cleanup['unit_removed'] is True and
            cleanup['properties_after']['LoadState'] == 'not-found' and
            cleanup['properties_after']['MainPID'] == '0' and
            runner['cleanup_complete'] is runner['runner_reaped'] is True and
            runner['returncode'] == 0 and runner['failure'] is None and
            runner['after_cleanup']['absent'] is True and
            all(not runner['after_cleanup'][key] for key in
                ('live_pids', 'zombie_pids', 'errors')),
            name + ': owned process or unit cleanup incomplete')
    require(all(finite(value) for value in (wrapper['started_t'], wrapper['ended_t'],
                runner['started_t'], runner['ended_t'])) and
            controller['started_t'] <= wrapper['started_t'] <= runner['started_t'] <
            runner['ended_t'] <= wrapper['ended_t'] <= controller['ended_t'],
            name + ': wrapper/runner is outside controller interval')
    enabled = name not in ('s01-as-off', 's04-al-off')
    require(protocol['policy_axis'] == 'request-partition-log' and
            protocol['partition'] == protocol['request_partition'] ==
            protocol['decode_partition_log_quiet'] == protocol['chunk_order'] == 0 and
            protocol['phase_diagnostics'] is True and
            protocol['effective_environment']['Q4T_MOE_SUPPLY_OBSERVER'] == str(int(enabled))
            and protocol['binary_sha256'] == plan['runtime_binary_sha256'] and
            protocol['host_cache_max_bytes'] == 16 << 30 and
            protocol['swap_max_bytes'] == 0 and
            protocol['mode'] == ('quality' if quality else 'performance'),
            name + ': runtime, observer, mode or host/cache budget differs')
    require(all(plan['tool_sha256'].get(key) == digest for key, digest in
                protocol['tool_sha256'].items()), name + ': runner tool identity differs')
    if not quality:
        require(http['partial_performance_matrix'] is True and
                http['full_offload_matrix_completed'] is False and
                http['performance_scope'] == SCOPE and
                protocol['diagnostic_scope'] == http['diagnostic_scope'] == SCOPE,
                name + ': diagnostic scope differs')
    return dict(service_started=True, decision=decision, controller=controller,
                wrapper=wrapper, http=http, cleanup=cleanup, runner=runner,
                protocol=protocol)


def specifications(name):
    if name == GROUPS[0]:
        return [dict(case='quality', bounded=False, envelope_count=1,
                     expected_requests=11, input_tokens=None)]
    predecessor = 8193 if name in ('s03-al-on', 's04-al-off') else 1024
    return [dict(case=f'{name}-r{position:02d}-' +
                     ('conditioning' if position == 0 else 'probe') +
                     f'-context-{predecessor if position == 0 else 1024}',
                 bounded=True, envelope_count=1, expected_requests=1,
                 input_tokens=predecessor if position == 0 else 1024,
                 position=position)
            for position in range(4)]


def bind_decision_sources(meta, completed, binaries):
    for name, state in completed.items():
        for path, digest in state['decision']['source_sha256'].items():
            candidate = Path(path)
            require(candidate.name not in RAW_NAMES,
                    'HTTP decision unexpectedly preconsumed raw resource stream')
            if candidate.suffix == '.bin':
                trace_root = EVIDENCE / name / 'http/trace'
                require(candidate.is_absolute() and candidate.parent == trace_root and
                        candidate.resolve().parent == trace_root.resolve() and
                        not candidate.is_symlink() and candidate.is_file() and
                        re.fullmatch(r'(request-[0-9]+|command|environment)\.bin',
                                     candidate.name) and candidate.stat().st_size <= 128 << 20,
                        'refuse non-trace binary or unbounded source')
                stat = candidate.stat()
                signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                with candidate.open('rb') as stream:
                    actual = hashlib.file_digest(stream, 'sha256').hexdigest()
                stat = candidate.stat()
                require(signature == (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                        and actual == digest, 'completed trace binding changed')
                binaries[str(candidate)] = {'sha256': actual, 'signature': signature,
                    'scope': 'Integrity hash only; no route or model payload parsing'}
            else:
                meta.bind(candidate, digest, candidate.suffix == '.json')


def enrich(meta, name, result, completed):
    metrics = completed['decision']['metrics']
    requests = []
    for spec in specifications(name):
        boundaries = meta.json(EVIDENCE / name / 'http' / spec['case'] /
                               'request-boundaries.json')
        for boundary in boundaries:
            metric = metrics[len(requests)]
            require(boundary['response_id'] == metric['response_id'] and
                    boundary['actual_input'] == metric['actual_input'] and
                    boundary['actual_output'] == metric['actual_output'] and
                    type(boundary['success']) in (bool, int) and
                    boundary['success'] == 1 and
                    boundary['http_timing']['within_client_boundaries'] is True,
                    name + ': resource/HTTP request identity differs')
            requests.append({'group_id': name, 'response_id': metric['response_id'],
                'actual_input': metric['actual_input'], 'actual_output': metric['actual_output'],
                'http_timing': boundary['http_timing'],
                'client_before': boundary['client_before'],
                'client_after': boundary['client_after'],
                'resource_envelope_label': boundary['request_group'],
                'individual_controller_IO_available': name != GROUPS[0]})
    require(len(requests) == len(metrics) == result['request_count'],
            name + ': request-to-resource coverage differs')
    for envelope in result['client_envelopes']:
        envelope.update(group_id=name,
            observer_enabled=name not in ('s01-as-off', 's04-al-off'),
            pid_storage_read_bytes=envelope['controller_counter_deltas']['pid_read_bytes'],
            pid_logical_rchar_bytes=envelope['controller_counter_deltas']['pid_rchar'],
            cgroup_read_bytes_by_device={key: value for key, value in
                envelope['controller_counter_deltas'].items() if key.startswith('cg_rbytes:')},
            device_read_byte_bounds_by_device={key: value for key, value in
                envelope['sample_counter_bounds'].items() if key.startswith('device_read_bytes:')},
            per_request_memory_peak_bytes=None,
            per_request_memory_status='NOT_DERIVED_BY_REUSED_GROUP_PEAK_READER')
    result['memory']['scope'] = 'Whole service lifecycle; not per-request peaks'
    result['memory']['required_host_cache_max_bytes'] = 16 << 30
    result['http_request_windows'] = requests
    result['PSI_status'] = result['inspection']['PSI_status']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    require(output.is_relative_to(R) and output.suffix == '.json' and
            not output.exists(), 'resource output must be a new JSON under R')
    report = dict(schema=1, scope=SCOPE, started_t=time.time(), ended_t=None,
        status='NOT_STARTED', audit_complete=False, raw_resources_read=False,
        performance_acceptance=False, physical_union_peak_bytes=None,
        whole_physical_RAM_54GB='INDETERMINATE')
    meta, source, binaries = None, None, {}
    with output.open('x') as destination:
        try:
            base = load_checked(BASE, BASE_SHA, '_observer_resource_base')
            meta = base.Metadata()
            plan_path = R / 'observer-execution-plan.json'
            plan = meta.json(plan_path, args.plan_sha256)
            require(plan['group_ids'] == list(GROUPS) and plan['service_count'] == 5
                    and plan['http_count'] == 27, 'resource fixed scope differs')
            required = ((BASE, BASE_SHA), (CORE, CORE_SHA), (RECEIPT, RECEIPT_SHA))
            for path, digest in required:
                require(plan['frozen_files'].get(str(path)) == digest,
                        'resource dependency absent from execution freeze: ' + str(path))
            require(str(Path(__file__).resolve()) in plan['frozen_files'],
                    'resource adapter absent from execution freeze')
            for path, digest in plan['frozen_files'].items():
                require(Path(path).is_absolute() and Path(path).name not in RAW_NAMES,
                        'invalid frozen input path')
                meta.bind(path, digest)
            receipt = meta.json(RECEIPT, RECEIPT_SHA)
            log = RECEIPT.with_name('self-test.log')
            require(receipt['returncode'] == 0 and receipt['script_sha256'] ==
                    receipt['script_sha256_after'] == CORE_SHA and
                    plan['frozen_files'].get(str(log)) == receipt['log_sha256'],
                    'old finite arithmetic receipt is not bound')
            meta.bind(log, receipt['log_sha256'])
            starts = {path.name.removesuffix('-controller-start.json') for path in
                      R.glob('*-controller-start.json') if
                      re.match(r'^[qs][0-9]+-', path.name)}
            directories = {path.name for path in EVIDENCE.iterdir() if path.is_dir()}
            require(starts == directories == set(GROUPS),
                    'started services/evidence differ from exact five cells')
            completed = {name: terminal(meta, name, plan, args.plan_sha256,
                                         report['started_t']) for name in GROUPS}
            for previous, current in zip(GROUPS, GROUPS[1:]):
                require(completed[previous]['decision']['recorded_t'] <=
                        completed[current]['controller']['started_t'],
                        'service order or completed prerequisite differs')
            bind_decision_sources(meta, completed, binaries)
            core = load_checked(CORE, CORE_SHA, '_observer_resource_core')
            source = core.Sources(EVIDENCE)
            base.EVIDENCE = EVIDENCE
            base.case_specifications = specifications
            report.update(plan_sha256=args.plan_sha256,
                runtime_source_commit=plan['runtime_source_commit'],
                runtime_binary_sha256=plan['runtime_binary_sha256'],
                all_service_terminals={name: state['controller']['ended_t']
                                       for name, state in completed.items()},
                method=dict(adapter_path=str(BASE), adapter_sha256=BASE_SHA,
                    core_path=str(CORE), core_sha256=CORE_SHA,
                    reused_functions=['Metadata', 'audit_envelope',
                        'audit_completed_group', 'Sources', 'read_samples',
                        'controller_delta', 'select_brackets', 'bounded_delta'],
                    changed_bindings=['adapter.EVIDENCE', 'adapter.case_specifications'],
                    reused_synthetic_examples=8, new_tests_executed=0,
                    reuse_scope='Unchanged arithmetic and raw reader; old tests do '
                        'not certify this new admission/identity adaptation.',
                    max_file_bytes=core.MAX_FILE_BYTES,
                    max_total_raw_bytes=core.MAX_TOTAL_BYTES,
                    max_rows_per_group=core.MAX_ROWS,
                    no_missing_as_zero=True, no_interpolation=True,
                    no_stale_carry_forward=True, raw_resource_parse_passes=1,
                    model_payload_reads=False, live_system_GPU_queries=False,
                    quality_resource_envelopes=1, diagnostic_resource_envelopes=16))
            report['raw_resources_read'] = True
            report['groups'] = {}
            for name in GROUPS:
                result = base.audit_completed_group(core, source, meta, name, completed[name])
                report['groups'][name] = enrich(meta, name, result, completed[name])
            report['http_request_count'] = sum(row['request_count']
                                               for row in report['groups'].values())
            report['client_envelope_count'] = sum(row['client_envelope_count']
                                                  for row in report['groups'].values())
            require(report['http_request_count'] == 27 and
                    report['client_envelope_count'] == 17,
                    'resource windows fail exact 27 HTTP/17 client envelope coverage')
            source.stable()
            meta.stable()
            for path, item in binaries.items():
                stat = Path(path).stat()
                require(tuple(item['signature']) ==
                        (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns),
                        'bound completed trace changed during resource audit')
            report['audit_complete'] = all(row['inspection']['all_summary_checks_match']
                and row['inspection']['identity_contracts_passed']
                for row in report['groups'].values())
            report['status'] = ('RAW_RESOURCE_EVIDENCE_REVIEWED; KNOWN_GAPS_RETAINED'
                if report['audit_complete'] else 'INCOMPLETE_OR_INVALID_EVIDENCE')
            report['interpretation_limits'] = [
                'Five services are the repetition units. No old service is pooled '
                    'and no per-position observation is an independent service replicate.',
                'PID storage read_bytes, logical rchar, cgroup I/O and partition '
                    'device counters remain separate; device background traffic '
                    'and parent disks are never added or attributed to expert SSD reads.',
                'I/O envelopes include client launch/teardown. Eleven quality HTTP '
                    'windows share one controller envelope and have no per-question I/O.',
                'Missing samples, PSI, counter resets, cgroup plateaus and bracket '
                    'uncertainty remain explicit unknowns; no interpolation or fake zeros.',
                'memory.current sampled peak and memory.peak charge peak are distinct '
                    'service-lifecycle observations, each compared to 16GiB separately.',
                'NVIDIA, cgroup, process and file-cache views overlap and have '
                    'noncoincident peaks; physical RAM54GB stays INDETERMINATE.',
                'This does not divide exclusive waiting time, establish pure observer '
                    'overhead, or accept a performance improvement.']
        except (OSError, ValueError, KeyError, TypeError, IndexError, AssertionError) as error:
            report.update(status='INCOMPLETE_OR_INVALID_EVIDENCE',
                          failure=type(error).__name__ + ': ' + str(error))
        report.update(ended_t=time.time(),
            metadata_source_sha256=meta.records if meta else {},
            source_sha256=source.records if source else {},
            trace_binary_source_bindings=binaries,
            raw_bytes_consumed=source.total_bytes if source else 0,
            metadata_bytes_consumed=meta.bytes_consumed if meta else 0)
        json.dump(report, destination, indent=2, allow_nan=False)
        destination.write('\n')
    print(json.dumps({key: report[key] for key in
                     ('status', 'audit_complete', 'raw_resources_read')}))
    return int(not report['audit_complete'])


if __name__ == '__main__':
    raise SystemExit(main())
