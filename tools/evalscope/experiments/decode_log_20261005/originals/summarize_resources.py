"""Summarize a completed frozen resource audit without rereading raw samples.

Prepared only. Root runs this after the resource audit finishes. Missing audit
fields remain unknown; in particular, syscr and software shape I/O were not
retained by the immutable resource core. This is not an independent raw audit.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import time

ROOT = Path(__file__).resolve().parent
STAGES = ('quality-c', 'history-a', 'history-b', 'history-c',
          'matrix-a', 'matrix-c')
LENGTHS = (1024, 4096, 8192, 45056, 204800, 261887)
AUDITOR_SHA = '10959f8021c7e2f6479ee77c8d163484054839a72fb172153bd723813f7ffde7'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_bound(path, expected):
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    require(digest == expected, 'SHA mismatch: ' + str(path))
    return json.loads(raw), digest


def known_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def counter_samples(rows, metric):
    result = []
    for row in rows:
        item = row['controller_counter_deltas'].get(metric)
        if item is None:
            item = {'value': None, 'reason': 'not_retained_by_frozen_audit'}
        value = item['value']
        require(value is None or type(value) is int and value >= 0,
                'invalid controller byte delta')
        result.append({'label': row['label'], 'value': value,
                       'reason': item.get('reason')})
    return result


def compare_counter(a_rows, c_rows, metric):
    a, c = counter_samples(a_rows, metric), counter_samples(c_rows, metric)
    means = {}
    for arm, rows in [('A', a), ('C', c)]:
        values = [row['value'] for row in rows]
        means[arm] = (statistics.mean(values)
                      if all(value is not None for value in values) else None)
    known = means['A'] is not None and means['C'] is not None
    return {'A_samples': a, 'C_samples': c, 'arithmetic_means': means,
            'C_minus_A': means['C'] - means['A'] if known else None,
            'percent_change': ((means['C'] / means['A'] - 1) * 100
                               if known and means['A'] != 0 else None),
            'mean_status': 'COMPLETE' if known else 'UNKNOWN_INCOMPLETE_SAMPLES',
            'partial_mean_used': False,
            'percentage_status': ('DEFINED' if known and means['A'] != 0 else
                                  'UNKNOWN_OR_ZERO_BASELINE')}


def io_visibility(envelopes):
    keys = sorted({key for row in envelopes for key in row['cgroup_vs_PID']})
    devices = {}
    for key in keys:
        rows = [row['cgroup_vs_PID'].get(key) for row in envelopes]
        agreements = [row.get('exactly_agrees') if row else None for row in rows]
        plateaus = [row.get('plateau_with_positive_PID') if row else None
                    for row in rows]
        devices[key] = {
            'envelopes': len(rows),
            'agrees_with_PID': sum(value is True for value in agreements),
            'disagrees_with_PID': sum(value is False for value in agreements),
            'agreement_unknown': sum(value is None for value in agreements),
            'zero_cgroup_delta_with_positive_PID':
                sum(value is True for value in plateaus),
            'plateau_unknown': sum(value is None for value in plateaus)}
    metrics = sorted({key for row in envelopes for key in row['sample_counter_bounds']})
    bounds = {}
    for metric in metrics:
        statuses = [row['sample_counter_bounds'].get(metric, {}).get('status')
                    for row in envelopes]
        bounds[metric] = {'bounded': sum(value == 'BOUNDED' for value in statuses),
                          'unavailable_or_unbounded':
                              sum(value != 'BOUNDED' for value in statuses)}
    return {'cgroup_vs_PID_by_counter': devices,
            'cgroup_counter_keys_observed': bool(keys),
            'sample_bounds_by_counter': bounds,
            'client_envelope_count': len(envelopes),
            'device_counter_specs_are_separate': True,
            'absence_is_not_zero': True}


def summarize_group(group):
    inspection, memory = group['inspection'], group['memory']
    require(inspection['all_summary_checks_match'] is True and
            inspection['identity_contracts_passed'] is True,
            'unverified group resource interpretation')
    current, peak = memory['observed_current_peak_bytes'], memory['charge_peak_bytes']
    psi_missing = [row for row in inspection['cgroup_unknown_phase_reason_groups']
                   if row['field'] in ('memory.pressure', 'io.pressure')]
    return {
        'http_request_count': group['request_count'],
        'client_envelope_count': group['client_envelope_count'],
        'memory': memory,
        'observed_current_peak_exceeds_final_charge_peak':
            current > peak if known_number(current) and known_number(peak) else None,
        'memory_counter_comparison_limit':
            'Sample current maximum and final charge peak are separate observations; '
            'preserve any inconsistency without correction or a continuous upper-bound claim.',
        'observed_cgroup_counter_peaks_bytes': inspection['raw_resource_peaks_bytes'],
        'observed_limit_values': inspection['observed_limit_values'],
        'sampled_memory_and_swap_event_maxima': inspection['memory_and_swap_event_maxima'],
        'sampled_swap_peak_bytes': inspection['observed_swap_peak_bytes'],
        'sampled_NVIDIA_peak_bytes': inspection['sampled_NVIDIA_peak_bytes'],
        'sampling_counts': inspection['counts'],
        'gpu_status_counts': inspection['gpu_status_counts'],
        'sampling_window_statistics': inspection['window_statistics'],
        'PSI_status': inspection['PSI_status'],
        'PSI_missing_phase_reason_groups': psi_missing,
        'process_io_unknown_observations': inspection['process_io_unknown_exact'],
        'process_io_unknown_records_truncated': inspection['process_io_unknown_records_truncated'],
        'counter_discontinuities': inspection['counter_discontinuities'],
        'counter_discontinuities_truncated': inspection['counter_discontinuities_truncated'],
        'io_observability': io_visibility(group['client_envelopes']),
        'device_specs': inspection['device_specs'],
        'live_file_cache_peak_bytes': group['live_file_cache_peak_bytes'],
        'physical_union_peak_bytes': None,
        'whole_physical_RAM_54GB': 'INDETERMINATE',
        'system_swap_peak_bytes': None,
        'system_swap_scope': 'Not retained by this audit; cgroup swap is not whole-system swap.',
        'quality_attribution': ('One client batch of eleven HTTP requests; '
                                'no per-question controller I/O attribution'
                                if group['request_count'] == 11 else
                                'Per-client request envelopes include launch/teardown')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit-sha256', required=True)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'raw-resource-summary.json')
    args = parser.parse_args()
    output = args.output.resolve()
    require(output.is_relative_to(ROOT) and output.suffix == '.json' and
            not output.exists(), 'summary output must be new JSON under phase root')
    audit_path, plan_path = ROOT / 'raw-resource-audit.json', ROOT / 'execution-plan.json'
    audit, audit_sha = load_bound(audit_path, args.audit_sha256)
    plan, plan_sha = load_bound(plan_path, args.plan_sha256)
    require(audit['audit_complete'] is True and audit['raw_resources_read'] is True and
            audit['all_summary_recomputations_match'] is True and
            audit['all_identity_contracts_satisfied'] is True and
            audit['selected_groups'] == list(STAGES) and
            set(audit['groups']) == set(STAGES) and
            audit['http_request_count'] == 110 and audit['client_envelope_count'] == 100 and
            audit['whole_physical_RAM_54GB'] == 'INDETERMINATE' and
            audit['physical_union_peak_bytes'] is None and
            known_number(audit['ended_t']) and audit['ended_t'] <= time.time(),
            'audit is not completed full-scope resource evidence')
    metadata = audit['metadata_source_sha256']
    require(metadata[str(plan_path)]['sha256'] == plan_sha and
            metadata[str(ROOT / 'audit_resources.py')]['sha256'] == AUDITOR_SHA and
            plan['frozen_files'][str(ROOT / 'audit_resources.py')] == AUDITOR_SHA,
            'resource audit does not bind the frozen plan/adapter')
    groups = {}
    for stage in STAGES:
        group = audit['groups'][stage]
        decision = group['terminal_contract']['decision']
        require(decision['passed'] is True and decision['stage'] == stage and
                decision['plan_sha256'] == plan_sha and
                decision['runtime_source_commit'] == plan['runtime_source_commit'] and
                decision['runtime_binary_sha256'] == plan['runtime_binary_sha256'] and
                group['binary_sha256_recorded'] == plan['runtime_binary_sha256'],
                'group belongs to another runtime or failed')
        expected = 11 if stage == 'quality-c' else 21 if stage.startswith('history-') else 18
        require(group['request_count'] == expected and
                group['client_envelope_count'] == (1 if stage == 'quality-c' else expected),
                'unexpected group coverage')
        groups[stage] = summarize_group(group)
    tiers = []
    for length in LENGTHS:
        rows = {arm: [row for row in audit['groups']['matrix-' + arm.lower()]['client_envelopes']
                      if row['input_tokens'] == length] for arm in ('A', 'C')}
        require(all(len(values) == 3 for values in rows.values()), 'matrix tier lacks three envelopes')
        require(all([row['label'] for row in values] ==
                    [f'context-{length}:run{i}' for i in range(1, 4)]
                    for values in rows.values()), 'matrix envelope ordering differs')
        tiers.append({'input_tokens': length,
            'PID_read_bytes': compare_counter(rows['A'], rows['C'], 'pid_read_bytes'),
            'PID_rchar_bytes': compare_counter(rows['A'], rows['C'], 'pid_rchar'),
            'PID_syscr_calls': {'A': None, 'C': None, 'status': 'NOT_RETAINED_BY_FROZEN_AUDIT'},
            'software_shape_IO': {'A': None, 'C': None, 'status': 'NOT_RETAINED_BY_FROZEN_AUDIT'}})
    result = {
        'schema': 1, 'status': 'SUMMARY_OF_COMPLETED_AUDIT_WITH_LIMITATIONS',
        'created_t': time.time(), 'runtime_source_commit': plan['runtime_source_commit'],
        'runtime_binary_sha256': plan['runtime_binary_sha256'],
        'plan_sha256': plan_sha, 'audit_path': str(audit_path), 'audit_sha256': audit_sha,
        'summarizer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'groups': groups, 'matrix_tiers': tiers,
        'coverage': {'groups': 6, 'http_requests': 110, 'client_envelopes': 100},
        'metric_definitions': {
            'PID_read_bytes': 'Kernel-accounted process storage reads for all files; not expert-only physical SSD traffic.',
            'PID_rchar_bytes': 'Logical process read bytes, including cached reads; not storage-device bytes.',
            'PID_syscr_calls': 'Read-call count, not bytes; unavailable in this audit output.',
            'software_shape_IO': 'GPU loads/payload or software requested NVMe bytes and shape counters are distinct from PID/device readings and absent here.'},
        'interpretation_limits': [
            'Summary reads only the completed audit and frozen plan; no raw samples, endpoints, server logs or CSV are rescanned.',
            'Means require all three known samples. Missing values stay unknown; zero is retained only when explicitly observed.',
            'Client process envelopes include launch and teardown; quality has one envelope for eleven HTTP requests.',
            'Cgroup I/O plateaus or disagreements do not imply zero SSD activity; PID, cgroup, partition and parent-device counters are not added.',
            'memory.current, final memory.peak and NVIDIA peaks are independent observations, not a deduplicated physical union or continuous upper bound.',
            'OOM zero or cgroup swap zero cannot prove absence of memory pressure or whole-system swap; preserve memory.events max and all missing PSI observations.',
            'Resource/GPU/cache windows and unsampled gaps remain explicit. No stale GPU carry-forward or interpolation; endpoint-only cache is not a runtime peak.',
            'This derivative summary is not an independent raw audit, performance acceptance, root-cause attribution, or a new model validation.'],
        'whole_physical_RAM_54GB': 'INDETERMINATE', 'physical_union_peak_bytes': None,
        'performance_acceptance': False, 'new_model_requests': 0,
        'source_sha256': {str(audit_path): audit_sha, str(plan_path): plan_sha}}
    with output.open('x') as destination:
        json.dump(result, destination, ensure_ascii=False, indent=2, allow_nan=False)
        destination.write('\n')
    print(json.dumps({'status': result['status'], 'output': str(output),
                      'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
