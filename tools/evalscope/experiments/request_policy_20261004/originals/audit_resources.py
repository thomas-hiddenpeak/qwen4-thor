"""Offline resource audit for completed request-policy HTTP groups.

Prepared before release; execute only after root selects completed groups. No
models, tests, subprocesses, live system/GPU queries, or cache advice. The old
counter/window/CSV arithmetic is imported by immutable SHA. Its old group gate
and context-directory iterator are deliberately not called.
"""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
EVIDENCE = ROOT / 'source/.q4t-work/evidence'
CORE = ROOT.parent / 'offload-partition-runtime-20261003/audit_raw_resources_frozen.py'
CORE_SHA = '035f1f840d00350a7b6db62e1b8ef7f62a5fda02406dae91395d291a67652da4'
OLD_TEST = ROOT.parent / 'offload-autonomous-20261003/raw-audit-execution-01'
OLD_TEST_EXIT_SHA = '8fcf469238691ef2993e5c453ffee4df549efa25e15c210b0e5ff116b47f655f'
STAGES = ('quality-on', 'inheritance-off', 'inheritance-on',
          'matrix-off', 'matrix-on')
HISTORY = (16385, 8192, 8193, 1024, 45056, 4096, 8192)
MATRIX = (1024, 4096, 8192, 45056, 204800, 261887)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def signature(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


class Metadata:
    """Bind metadata once, separately from bounded single-pass raw streams."""
    def __init__(self):
        self.records = {}
        self.values = {}
        self.bytes_consumed = 0

    def bind(self, path, expected=None, decode=False):
        path = Path(path).resolve()
        require(path.suffix not in ('.safetensors', '.bin'),
                'refuse model payload source')
        key = str(path)
        if key not in self.records:
            before = signature(path)
            require(before[2] <= 256 << 20, 'metadata file bound exceeded')
            require(self.bytes_consumed + before[2] <= 1 << 30,
                    'metadata total read bound exceeded')
            raw = path.read_bytes()
            after = signature(path)
            require(before == after and len(raw) == before[2],
                    'metadata changed while reading: ' + key)
            self.records[key] = dict(sha256=hashlib.sha256(raw).hexdigest(),
                                     bytes=len(raw), stat=before)
            self.bytes_consumed += len(raw)
            if decode or path.suffix == '.json':
                self.values[key] = json.loads(raw)
        require(expected is None or self.records[key]['sha256'] == expected,
                'metadata SHA mismatch: ' + key)
        if decode:
            require(key in self.values, 'metadata already consumed without JSON decode')
            return self.values[key]
        return self.records[key]['sha256']

    def json(self, path, expected=None):
        return self.bind(path, expected, True)

    def stable(self):
        for path, record in self.records.items():
            require(signature(Path(path)) == tuple(record['stat']),
                    'metadata changed after reading: ' + path)


def stage_decision(meta, stage, plan, plan_sha):
    label = 'quality' if stage == 'quality-on' else stage
    decision = meta.json(ROOT / (label + '-decision.json'))
    require(decision['passed'] is True and decision['stage'] == stage and
            decision['plan_sha256'] == plan_sha and
            decision['runtime_binary_sha256'] == plan['runtime_binary_sha256'] and
            decision['runtime_source_commit'] == plan['runtime_source_commit'],
            stage + ': contract decision belongs to another runtime or failed')
    expected = 11 if stage == 'quality-on' else 21 if stage.startswith('inheritance-') else 18
    require(decision['request_count'] == expected, stage + ': wrong HTTP count')
    for path, digest in decision['source_sha256'].items():
        # This gate hashes completed evidence. JSON is decoded here so later
        # consumers can reuse it without rescanning large source files.
        meta.bind(path, digest, str(path).endswith('.json'))
    return decision


def terminal(meta, stage, plan, plan_sha, started_t):
    directory = EVIDENCE / stage
    wrapper = meta.json(directory / 'wrapper-exit.json')
    if not (directory / 'http/exit.json').exists():
        require(wrapper['runner_rc'] is None and wrapper['monitor_rc'] is None and
                wrapper['failure'] == 'cold payload gate failed; no service started',
                stage + ': group is active or has no terminal service evidence')
        gate = meta.json(directory / 'cache-gate.json')
        require(gate['cold_payload_established'] is False and
                not (directory / 'http/server.pid').exists(),
                stage + ': cold failure unexpectedly launched a service')
        return {'service_started': False, 'wrapper': wrapper, 'cold_gate': gate}
    decision = stage_decision(meta, stage, plan, plan_sha)
    controller = meta.json(ROOT / (stage + '-stage.json'))
    http = meta.json(directory / 'http/exit.json')
    cleanup = meta.json(directory / 'http/isolation/cleanup.json')
    runner = meta.json(directory / 'runner-process-group.json')
    protocol = meta.json(directory / 'protocol.json')
    require(controller['returncode'] == 0 and controller['failure'] is None and
            controller['cleanup_complete'] is True and
            finite(controller['ended_t']) and controller['ended_t'] <= started_t,
            stage + ': stage/controller is not terminal and clean')
    require(wrapper['runner_rc'] == wrapper['monitor_rc'] == http['server'] == 0 and
            wrapper['failure'] is http['failure'] is http['cleanup_failure'] is None and
            wrapper['cleanup_failed'] is False and
            wrapper['unit_after_cleanup']['LoadState'] == 'not-found' and
            http['http_output_checks_passed'] is True,
            stage + ': HTTP or wrapper is incomplete/failed')
    require(cleanup['stop_rc'] == 0 and cleanup['unit_removed'] is True and
            cleanup['properties_after']['LoadState'] == 'not-found' and
            cleanup['properties_after']['MainPID'] == '0' and
            runner['cleanup_complete'] is True and runner['runner_reaped'] is True and
            runner['returncode'] == 0 and runner['failure'] is None and
            runner['after_cleanup']['absent'] is True and
            all(not runner['after_cleanup'][key]
                for key in ('live_pids', 'zombie_pids', 'errors')),
            stage + ': owned process/unit cleanup incomplete')
    require(all(finite(value) for value in
                (wrapper['started_t'], wrapper['ended_t'], runner['started_t'],
                 runner['ended_t'], controller['started_t'])) and
            controller['started_t'] <= wrapper['started_t'] <= runner['started_t'] <
            runner['ended_t'] <= wrapper['ended_t'] <= controller['ended_t'],
            stage + ': group windows are not contained by controller')
    state = int(stage.endswith('-on'))
    require(protocol['policy_axis'] == 'request-partition' and
            protocol['partition'] == protocol['request_partition'] == state and
            protocol['chunk_order'] == 0 and not protocol['phase_diagnostics'] and
            not protocol['diagnostic_scope'] and not http['diagnostic_scope'] and
            protocol['binary_sha256'] == plan['runtime_binary_sha256'] and
            protocol['tool_sha256'] == plan['tool_sha256'],
            stage + ': wrong policy axis/runtime/tools')
    if stage == 'quality-on':
        require(http['completed'] == 11 and protocol['host_cache_max_bytes'] is None,
                'quality group must remain eleven requests without host/cache limit')
    elif stage.startswith('inheritance-'):
        require(http['completed'] == 21 and http['partial_performance_matrix'] is True and
                not http['full_offload_matrix_completed'] and
                http['performance_scope'] == 'request_partition_history_v1' and
                protocol['host_cache_max_bytes'] == 16 << 30 and
                protocol['swap_max_bytes'] == 0,
                'history group shape/host-cache budget mismatch')
    else:
        require(http['completed'] == 6 and http['full_offload_matrix_completed'] is True and
                wrapper['full_offload_matrix_completed'] is True and
                protocol['host_cache_max_bytes'] == 16 << 30 and
                protocol['swap_max_bytes'] == 0,
                'matrix incomplete or host-cache budget mismatch')
    return dict(service_started=True, decision=decision, controller=controller,
                wrapper=wrapper, http=http, cleanup=cleanup, runner=runner,
                protocol=protocol)


def case_specifications(stage):
    if stage == 'quality-on':
        return [dict(case='quality', bounded=False, envelope_count=1,
                     expected_requests=11, input_tokens=None)]
    if stage.startswith('inheritance-'):
        return [dict(case=f'sequence-r{round_index:02d}-p{position:02d}-context-{length}',
                     bounded=True, envelope_count=1, expected_requests=1,
                     input_tokens=length, round=round_index, position=position)
                for round_index in range(1, 4)
                for position, length in enumerate(HISTORY, 1)]
    return [dict(case=f'context-{length}', bounded=True, envelope_count=3,
                 expected_requests=3, input_tokens=length) for length in MATRIX]


def audit_envelope(core, samples, named, io_metrics, pid, ticks, cgroup,
                   label, before, after):
    start, end = before['unix_seconds'], after['unix_seconds']
    require(finite(start) and finite(end) and start < end,
            'invalid client envelope time')
    drift = abs((end - start) -
                (after['monotonic_seconds'] - before['monotonic_seconds']))
    require(finite(drift), 'invalid client monotonic clock')
    bracket, reason = core.select_brackets(samples, start, end)
    if drift > 0.01:
        bracket, reason = None, 'wall_monotonic_offset_drift_over_10ms'
    bounds = {metric: core.bounded_delta(samples, bracket, metric)
              for metric in io_metrics}
    first, last = named.get('before-' + label), named.get('after-' + label)
    exact = {metric: core.controller_delta(first, last, metric, pid, ticks, cgroup)
             for metric in io_metrics if not metric.startswith('device_')}
    pid_value = exact['pid_read_bytes']['value']
    cg_compare = {
        metric: dict(delta=value['value'], pid_delta=pid_value,
            exactly_agrees=(value['value'] == pid_value if
                value['value'] is not None and pid_value is not None else None),
            plateau_with_positive_PID=(value['value'] == 0 and pid_value > 0 if
                value['value'] is not None and pid_value is not None else None))
        for metric, value in exact.items() if metric.startswith('cg_rbytes:')}
    device_compare = {}
    pid_bounds = bounds['pid_read_bytes']
    for metric, value in bounds.items():
        if not metric.startswith('device_'):
            continue
        known = value['status'] == pid_bounds['status'] == 'BOUNDED'
        device_compare[metric] = dict(
            intervals_overlap=(max(value['lower_bytes'], pid_bounds['lower_bytes']) <=
                min(value['upper_bytes'], pid_bounds['upper_bytes']) if known else None),
            exact_controller_PID_within_device_bracket=(
                value['lower_bytes'] <= pid_value <= value['upper_bytes'] if
                value['status'] == 'BOUNDED' and pid_value is not None else None),
            whole_device_can_be_attributed_to_q4t=False)
    return dict(label=label, client_envelope=[start, end],
                wall_vs_monotonic_drift_seconds=drift, bracket=bracket,
                bracket_unknown_reason=reason, sample_counter_bounds=bounds,
                controller_counter_deltas=exact, cgroup_vs_PID=cg_compare,
                device_vs_PID=device_compare,
                scope='Client process envelope including launch/teardown; '
                      'not an exact HTTP-only interval')


def audit_completed_group(core, source, meta, stage, completed):
    directory = EVIDENCE / stage
    summary = meta.json(directory / 'memory/memory-peak.json')
    protocol = completed['protocol']
    identity = meta.json(directory / 'http/isolation/identity.json')
    require(summary['sampling_complete'] is True and summary['stop_reason'] == 'target_exited'
            and summary['prelaunch_sample_present'] is True,
            stage + ': incomplete monitor lifecycle')
    pid, ticks = summary['root_pid'], summary['root_start_ticks']
    require(pid == identity['pid'] == int(identity['properties']['MainPID']) and
            type(ticks) is int and ticks > 0, stage + ': PID generation mismatch')
    cgroup = '/sys/fs/cgroup' + identity['properties']['ControlGroup']
    require(cgroup == '/sys/fs/cgroup/system.slice/' + protocol['unit'] and
            protocol['monitor']['file_cache_mode'] == 'endpoints',
            stage + ': wrong monitor binding/cache observation policy')
    for filename in ('monitor_memory.py', 'resource_metrics.py'):
        meta.bind(directory / 'tools' / filename, protocol['tool_sha256'][filename])
    endpoints = list(source.jsonl(directory / 'http/isolation/endpoints.jsonl'))
    named = {row['label']: row for row in endpoints}
    require(len(named) == len(endpoints), 'duplicate controller endpoint label')
    samples, inspection = core.read_samples(source, directory, summary, cgroup)
    require(completed['wrapper']['started_t'] <= samples[0]['start_t'] <=
            samples[-1]['end_t'] <= completed['wrapper']['ended_t'],
            stage + ': resource stream lies outside wrapper window')
    for endpoint in endpoints:
        resources = endpoint['resources']
        require(completed['runner']['started_t'] <= resources['start_t'] <=
                resources['end_t'] <= completed['runner']['ended_t'],
                stage + ': controller resource window lies outside runner')
    metrics = sorted(set().union(*(row['metrics'].keys() for row in samples)))
    io_metrics = [key for key in metrics
                  if key.startswith(('pid_', 'cg_rbytes:', 'device_read_bytes:'))]
    require('pid_read_bytes' in io_metrics and 'pid_rchar' in io_metrics,
            'PID counter columns missing entirely')
    envelopes = []
    previous_end = None
    request_count = 0
    for spec in case_specifications(stage):
        case = directory / 'http' / spec['case']
        boundaries = meta.json(case / 'request-boundaries.json')
        require(len(boundaries) == spec['expected_requests'],
                'client boundary count differs from frozen cases')
        require(len({b['response_id'] for b in boundaries}) == len(boundaries),
                'duplicate response ID within case')
        request_count += len(boundaries)
        selected = boundaries if spec['bounded'] else [boundaries[0]]
        for index, boundary in enumerate(selected, 1):
            label = spec['case'] + (f':run{index}' if spec['bounded'] else '')
            client_path = case / (f'run-{index}' if spec['bounded'] else '') / 'client-exit.json'
            client = meta.json(client_path)
            before, after = boundary['client_before'], boundary['client_after']
            require(before == client['before'] and after == client['after'] and
                    client['returncode'] == 0 and boundary['request_group'] == label and
                    boundary['request_index'] == index,
                    'request boundary differs from client envelope')
            require(before['expected_requests'] == (1 if spec['bounded'] else 11),
                    'client batch count differs')
            if not spec['bounded']:
                require(all(b['client_before'] == before and b['client_after'] == after and
                            b['request_group'] == label and b['request_index'] == n
                            for n, b in enumerate(boundaries, 1)),
                        'quality rows do not share exactly one batch client')
            if previous_end is not None:
                require(previous_end <= before['unix_seconds'],
                        'serial client envelopes overlap or are reordered')
            require(completed['runner']['started_t'] <= before['unix_seconds'] <
                    after['unix_seconds'] <= completed['runner']['ended_t'],
                    'client window outside owning runner')
            previous_end = after['unix_seconds']
            row = audit_envelope(core, samples, named, io_metrics, pid, ticks,
                                 cgroup, label, before, after)
            row.update({key: spec[key] for key in ('input_tokens', 'round', 'position')
                        if key in spec})
            row['http_request_count'] = 1 if spec['bounded'] else 11
            row['per_request_IO_attribution'] = spec['bounded']
            if not spec['bounded']:
                row['scope'] = 'Whole quality client batch containing eleven HTTP requests; '
                row['scope'] += 'no per-question controller I/O attribution'
            envelopes.append(row)
    require(request_count == completed['decision']['request_count'],
            'resource envelopes do not account for every accepted HTTP request')
    require(endpoints and endpoints[-1]['label'] == 'before_unit_cleanup',
            'last retained cgroup endpoint missing')
    final = endpoints[-1]
    observations = final['resources']['cgroup']['observations']
    limit = observations['memory.max']['value']
    peak = observations['memory.peak']['value']
    current_peak = inspection['raw_resource_peaks_bytes']['memory.current']
    require(final['resources']['cgroup']['consistent_identity'] is True,
            'final cgroup generation is unknown')
    initial_path = cgroup
    require(final['resources']['cgroup']['path'] == initial_path,
            'final cgroup belongs to another service')
    charge_excess = peak - limit if type(peak) is type(limit) is int else None
    current_excess = (current_peak - limit if type(current_peak) is type(limit) is int
                      else None)
    cg_agree = [value['exactly_agrees'] for row in envelopes
                for value in row['cgroup_vs_PID'].values()]
    return dict(request_count=request_count, client_envelope_count=len(envelopes),
        terminal_contract=completed, initial_identity=identity,
        binary_sha256_recorded=protocol['binary_sha256'], inspection=inspection,
        client_envelopes=envelopes, memory=dict(
            memory_max_bytes=limit, charge_peak_bytes=peak,
            observed_current_peak_bytes=current_peak,
            charge_peak_minus_max_bytes=charge_excess,
            observed_current_peak_minus_max_bytes=current_excess,
            final_memory_events=observations['memory.events'],
            final_swap_current=observations['memory.swap.current'],
            final_swap_peak=observations['memory.swap.peak'],
            final_swap_max=observations['memory.swap.max'],
            final_swap_events=observations['memory.swap.events'],
            final_counter_identity=final['resources']['cgroup']['identity'],
            final_counter_window=[final['resources']['start_t'], final['resources']['end_t']],
            sampled_NVIDIA_peak_bytes=inspection['sampled_NVIDIA_peak_bytes'],
            independent_observations_never_added=True),
        cgroup_IO_agreement=(all(cg_agree) if cg_agree and
                            all(value is not None for value in cg_agree) else None),
        live_file_cache_peak_bytes=None, physical_union_peak_bytes=None,
        whole_physical_RAM_54GB='INDETERMINATE',
        no_parent_disk_plus_partition_sum=True,
        interpretation='PID read_bytes covers all q4t file reads, not experts only. '
          'Cgroup I/O divergence/plateaus remain explicit. Device partitions include '
          'background traffic and are separate from parent disks. NVIDIA/process/cache '
          'views overlap; their separate peaks are not a physical memory union.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--groups', required=True,
                        help='Explicit comma-separated completed stages in execution order')
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    selected = args.groups.split(',')
    require(selected and selected == [stage for stage in STAGES if stage in selected],
            'groups must be unique, supported, and in frozen execution order')
    output = args.output.resolve()
    require(output.is_relative_to(ROOT) and output.suffix == '.json' and
            not output.exists(), 'new audit output must be JSON under phase root')
    report = dict(schema=1, started_t=time.time(), ended_t=None,
                  audit_complete=False, raw_resources_read=False,
                  selected_groups=selected, performance_acceptance=False,
                  whole_physical_RAM_54GB='INDETERMINATE',
                  physical_union_peak_bytes=None)
    meta, source = Metadata(), None
    with output.open('x') as destination:
        try:
            plan = meta.json(ROOT / 'execution-plan.json', args.plan_sha256)
            meta.bind(ROOT / 'plan.json', plan['phase_plan_sha256'])
            meta.bind(CORE, CORE_SHA)
            previous = meta.json(OLD_TEST / 'self-test-exit.json', OLD_TEST_EXIT_SHA)
            meta.bind(OLD_TEST / 'self-test.log', previous['log_sha256'])
            require(previous['returncode'] == 0 and previous['script_sha256'] ==
                    previous['script_sha256_after'] == CORE_SHA,
                    'reused examples do not bind immutable core arithmetic')
            meta.bind(__file__)
            # Every started group must be terminal before any raw resource read,
            # even if caller selects only an earlier group for this output.
            started = [stage for stage in STAGES if (EVIDENCE / stage).exists()]
            require('quality-on' in started and all(stage in started for stage in selected),
                    'selected evidence unavailable or first quality not started')
            completed = {stage: terminal(meta, stage, plan, args.plan_sha256,
                                         report['started_t']) for stage in started}
            require(all(completed[stage]['service_started'] for stage in selected),
                    'selected group never started a service')
            require(completed['quality-on']['service_started'],
                    'fixed quality must have completed before resource analysis')
            report['all_started_group_terminals'] = {
                stage: dict(service_started=record['service_started'],
                            ended_t=record.get('controller', {}).get('ended_t'))
                for stage, record in completed.items()}
            # The speed decision may be NO_GO; resource evidence remains useful.
            for kind in ('inheritance', 'matrix'):
                pair = [kind + '-off', kind + '-on']
                if all(stage in completed and completed[stage]['service_started']
                       for stage in pair):
                    decision = meta.json(ROOT / (kind + '-decision.json'))
                    require(decision['decision'] in ('PASS_SCREENING', 'NO_GO') and
                            decision['evidence_contracts_passed'] is True and
                            decision['plan_sha256'] == args.plan_sha256 and
                            decision['runtime_binary_sha256'] == plan['runtime_binary_sha256'],
                            'pair has no terminal valid screening decision')
                    for path, digest in decision['source_sha256'].items():
                        meta.bind(path, digest, str(path).endswith('.json'))
            spec = importlib.util.spec_from_file_location('request_policy_resource_core', CORE)
            core = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(core)
            source = core.Sources(EVIDENCE)
            report['method'] = dict(core_path=str(CORE), core_sha256=CORE_SHA,
                reused_synthetic_examples=8, new_tests_executed=0,
                reuse_scope='Exact unchanged counter/window/CSV arithmetic only; '
                            'not certification of this adapter or its new evidence.',
                selected_groups=selected, max_file_bytes=core.MAX_FILE_BYTES,
                max_total_raw_bytes=core.MAX_TOTAL_BYTES,
                max_rows_per_group=core.MAX_ROWS,
                boundary_uncertainty_limit_seconds=core.MAX_BOUNDARY_UNCERTAINTY_S,
                clock_drift_limit_seconds=0.01, no_interpolation=True,
                no_missing_as_zero=True, no_stale_carry_forward=True,
                raw_resource_read_passes=1, model_payload_reads=False,
                live_system_GPU_queries=False, performance_gate_changes=False)
            report['raw_resources_read'] = True
            report['groups'] = {
                stage: audit_completed_group(core, source, meta, stage, completed[stage])
                for stage in selected}
            source.stable()
            meta.stable()
            report['all_summary_recomputations_match'] = all(
                group['inspection']['all_summary_checks_match']
                for group in report['groups'].values())
            report['all_identity_contracts_satisfied'] = all(
                group['inspection']['identity_contracts_passed']
                for group in report['groups'].values())
            report['audit_complete'] = (report['all_summary_recomputations_match'] and
                                       report['all_identity_contracts_satisfied'])
            report['status'] = ('RAW_RESOURCE_EVIDENCE_REVIEWED; KNOWN_GAPS_RETAINED'
                if report['audit_complete'] else 'INCOMPLETE_OR_INVALID_EVIDENCE')
        except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
            report.update(status='INCOMPLETE_OR_INVALID_EVIDENCE',
                          failure=type(error).__name__ + ': ' + str(error))
        report.update(ended_t=time.time(), metadata_source_sha256=meta.records,
                      source_sha256=source.records if source else {},
                      raw_bytes_consumed=source.total_bytes if source else 0,
                      metadata_bytes_consumed=meta.bytes_consumed)
        json.dump(report, destination, indent=2, allow_nan=False)
        destination.write('\n')
    print(json.dumps({key: report[key] for key in
                     ('status', 'audit_complete', 'raw_resources_read')}))
    return int(not report['audit_complete'])


if __name__ == '__main__':
    raise SystemExit(main())
