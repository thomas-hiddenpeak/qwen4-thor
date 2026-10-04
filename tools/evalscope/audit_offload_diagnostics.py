"""Join instrumented HTTP responses and phase evidence; never accept speed.

This reads completed evidence only. A valid result establishes the bounded
diagnostic record, not causality, performance noninferiority or physical RAM.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re

from offload_policy import (DIAGNOSTIC_SCOPE, RUN_TOOLS, bound_file,
                            partition_path_evidence, policy_environment,
                            read_bound_json, require)

PREFIX = '[q4t][offload_diag] '
SCHEMA = 'q4t.offload_phase.v1'
LENGTHS = [1024, 4096, 8192]
CAPACITY = {'max_len': 262144, 'max_seq': 1, 'max_prefill': 8192}
STAT_KEYS = set(('resolve_calls expert_lookups hits misses loads evictions '
    'load_bytes nvme_read_bytes shape_single_lookups shape_single_misses '
    'shape_multi_lookups shape_multi_misses l2_hits l2_misses l2_evictions '
    'l2_shape_single_hits l2_shape_single_misses l2_shape_single_evictions '
    'l2_shape_multi_hits l2_shape_multi_misses l2_shape_multi_evictions '
    'mirror_hits mirror_writebacks mirror_skips pread_merge_runs '
    'pread_merge_experts').split())
BYTE_COUNTERS = {'load_bytes', 'nvme_read_bytes'}
TIMING_KEYS = {group + suffix for group in ('stage', 'pread', 'swz', 'phase1',
    'd2h', 'shape_single_stage', 'shape_single_pread', 'shape_single_phase1',
    'pread_merge') for suffix in ('_count', '_ns', '_max_ns')}
CACHE_KEYS = {'layer', 'slot_experts', 'slot_ticks', 'slot_protected',
              'l2_experts', 'l2_ticks', 'mirror_experts', 'slot_clock',
              'l2_clock', 'mirror_cursor'}


def unsigned(value):
    return type(value) is int and 0 <= value < 1 << 64


def counter_delta(before, after, label, byte_counters=()):
    require(isinstance(before, dict) and isinstance(after, dict) and
            set(before) == set(after),
            f'{label}: counter fields changed')
    def valid(key, value):
        return (type(value) in (int, float) and math.isfinite(value) and
                value >= 0) if key in byte_counters else unsigned(value)
    require(all(valid(key, value) for row in (before, after)
                for key, value in row.items()),
            f'{label}: counters must be unsigned integers')
    require(all(after[key] >= value for key, value in before.items()),
            f'{label}: counter decreased')
    return {key: after[key] - value for key, value in before.items()}


def phase_interval(before, after):
    start, end = before['monotonic_ns'], after['monotonic_ns']
    a, b = before['pid_read_bytes'], after['pid_read_bytes']
    read_delta = b - a if a is not None and b is not None and b >= a else None
    return {
        'elapsed_ns': end - start,
        'elapsed_includes_instrumentation': True,
        'pid_read_bytes': read_delta,
        'pid_read_status': ('observed_process_counter_delta'
            if read_delta is not None else 'unknown_or_counter_decreased'),
        'pid_read_scope': 'all process files; not expert-only physical SSD',
        'stats': counter_delta(before['stats'], after['stats'], 'phase stats',
                               BYTE_COUNTERS),
        'timing': timing_delta(before['timing'], after['timing']),
    }


def timing_delta(before, after):
    if before is None or after is None:
        return None
    require(before.get('enabled') is True and after.get('enabled') is True,
            'timing disabled in diagnostic evidence')
    a = {key: value for key, value in before.items() if key != 'enabled'}
    b = {key: value for key, value in after.items() if key != 'enabled'}
    require(set(a) == set(b) == TIMING_KEYS, 'timing fields incomplete')
    delta = counter_delta(a, b, 'timing')
    # A cumulative maximum is not a per-interval maximum or subtractable sum.
    return {'count_and_sum_deltas': {key: value for key, value in delta.items()
                                    if 'max' not in key},
            'cumulative_maxima_at_end': {key: value for key, value in b.items()
                                       if 'max' in key}}


def audit_cache(layers):
    require([layer.get('layer') for layer in layers] == list(range(48)),
            'cache layer IDs missing, duplicate or unordered')
    for layer in layers:
        require(set(layer) == CACHE_KEYS, 'cache layer fields incomplete')
        for key, count in (('slot_experts', 256), ('l2_experts', 16),
                           ('mirror_experts', 8)):
            values = layer[key]
            require(isinstance(values, list) and len(values) == count and
                    all(type(value) is int and -1 <= value < 512
                        for value in values), 'cache expert range/size differs')
            occupied = [value for value in values if value >= 0]
            require(len(set(occupied)) == len(occupied),
                    'duplicate expert within a cache level')
        for prefix, count in (('slot', 256), ('l2', 16)):
            require(unsigned(layer[prefix + '_clock']), 'invalid cache clock')
            ticks = layer[prefix + '_ticks']
            require(isinstance(ticks, list) and len(ticks) == count and
                    all(unsigned(tick) and tick <= layer[prefix + '_clock']
                        for tick in ticks), 'invalid cache ticks')
        require(len(layer['slot_protected']) == 256 and
                all(type(value) is int and value in (0, 1)
                    for value in layer['slot_protected']), 'invalid protection flags')
        require(type(layer['mirror_cursor']) is int and
                0 <= layer['mirror_cursor'] < 8, 'invalid mirror cursor')


def audit_record(record, response):
    require(record.get('schema') == SCHEMA, 'unknown phase schema')
    require(record.get('request_id') == response.get('response_id') and
            response.get('response_id_valid') is True, 'response/phase ID differs')
    require(record.get('outcome') == 'success' and
            record.get('completeness') == 'complete',
            'failed, cancelled or partial phase evidence')
    require(record.get('input_tokens') == response['actual_input'] and
            record.get('output_tokens') == response['actual_output'],
            'phase/HTTP token counts differ')
    require(all(type(response[key]) in (int, float) and
                math.isfinite(response[key]) and response[key] > 0
                for key in ('ttft', 'latency')) and
            response['latency'] >= response['ttft'], 'invalid HTTP timing')
    forwards = record.get('decode_forwards_completed')
    require(type(forwards) is int and forwards == response['actual_output'] - 1,
            'decode forward count differs from ordinary generation')
    snapshots = record.get('snapshots')
    prefixes = [count for count in (1, 8, 32) if count <= forwards]
    expected = [('prefill_begin', 0), ('prefill_end_decode_begin', 0),
                *[('decode_prefix', count) for count in prefixes],
                ('inference_end', forwards)]
    require(isinstance(snapshots, list) and
            [(s.get('event'), s.get('decode_forward_count')) for s in snapshots]
            == expected, 'missing, duplicate or unordered phase snapshots')
    previous_end = None
    previous_realtime_end = None
    for snapshot in snapshots:
        for key in ('monotonic_ns', 'realtime_ns', 'capture_end_monotonic_ns',
                    'capture_end_realtime_ns', 'capture_ns'):
            require(unsigned(snapshot.get(key)), 'invalid snapshot clock: ' + key)
        require(snapshot['capture_end_monotonic_ns'] >= snapshot['monotonic_ns']
                and snapshot['capture_end_realtime_ns'] >= snapshot['realtime_ns']
                and snapshot['capture_ns'] ==
                snapshot['capture_end_monotonic_ns'] - snapshot['monotonic_ns'],
                'invalid capture interval')
        require(previous_end is None or snapshot['monotonic_ns'] >= previous_end,
                'phase capture order reverses')
        require(previous_realtime_end is None or
                snapshot['realtime_ns'] >= previous_realtime_end,
                'phase realtime order reverses')
        previous_end = snapshot['capture_end_monotonic_ns']
        previous_realtime_end = snapshot['capture_end_realtime_ns']
        require(snapshot.get('gpu_work_complete') is True,
                'phase snapshot lacks completed GPU work')
        require(snapshot.get('pid_read_bytes_scope') == 'process_all_files' and
                (snapshot.get('pid_read_bytes') is None or
                 unsigned(snapshot['pid_read_bytes'])), 'invalid PID read field')
        require(isinstance(snapshot.get('stats'), dict) and
                set(snapshot['stats']) == STAT_KEYS, 'stats fields incomplete')
        counter_delta(snapshot['stats'], snapshot['stats'], 'stats', BYTE_COUNTERS)
        require(snapshot.get('timing') is not None,
                'enabled diagnostic timing missing')
        timing_delta(snapshot.get('timing'), snapshot.get('timing'))
        full = snapshot['event'] != 'decode_prefix'
        require(snapshot.get('cache_state') == ('full' if full else 'omitted'),
                'cache snapshot scope differs')
        require((isinstance(snapshot.get('layers'), list) and
                 len(snapshot['layers']) == 48) if full else
                snapshot.get('layers') is None, 'cache layer coverage differs')
        if full:
            audit_cache(snapshot['layers'])
    for before, after in zip(snapshots, snapshots[1:]):
        counter_delta(before['stats'], after['stats'], 'consecutive stats',
                      BYTE_COUNTERS)
        timing_delta(before['timing'], after['timing'])
    begin, boundary, end = snapshots[0], snapshots[1], snapshots[-1]
    timing = response['timing']
    require(timing['within_client_boundaries'] is True,
            'HTTP timing outside recorded client envelope')
    http_start = timing['start_monotonic_seconds'] * 1e9
    http_end = timing['end_monotonic_seconds'] * 1e9
    require(http_start <= begin['monotonic_ns'] <= end['capture_end_monotonic_ns']
            <= http_end, 'server phase timestamps outside HTTP interval')
    return {'response_id': response['response_id'],
            'actual_input': response['actual_input'],
            'actual_output': response['actual_output'],
            'prompt_sha256': response['prompt_sha256'],
            'output_sha256': hashlib.sha256(response['text'].encode()).hexdigest(),
            'client_ttft_seconds': response['ttft'],
            'client_latency_seconds': response['latency'],
            'first_content_time_estimate_ns': http_start + response['ttft'] * 1e9,
            'first_content_caveat': 'evalscope first_chunk_latency; not a server '
                'token emission timestamp or a pure prefill measurement',
            'prefill': phase_interval(begin, boundary),
            'decode': phase_interval(boundary, end),
            'decode_prefixes': [{
                'decode_forward_count': item['decode_forward_count'],
                **phase_interval(boundary, item)} for item in snapshots[2:-1]],
            'capture_ns_total': sum(s['capture_ns'] for s in snapshots),
            'phase_record': record}


def read_phase_records(path, sources):
    digest, records, path_lines = hashlib.sha256(), {}, []
    with path.open('rb') as stream:
        for raw in stream:
            digest.update(raw)
            line = raw.decode('utf-8', errors='strict').rstrip('\n')
            if ('[q4t][residency][partition]' in line or
                    '[q4t][residency] chunk_order=' in line or
                    '[q4t][residency] partition=' in line or
                    '[q4t][offload_diag_config]' in line):
                path_lines.append(line)
            if '[q4t][offload_diag]' not in line:
                continue
            require(line.startswith(PREFIX), 'malformed diagnostic log prefix')
            record = json.loads(line[len(PREFIX):])
            identity = record.get('request_id')
            require(isinstance(identity, str) and identity not in records,
                    'missing or duplicate diagnostic request ID')
            records[identity] = record
    sources[str(path.resolve())] = digest.hexdigest()
    return records, '\n'.join(path_lines)


def audit_evidence(baseline, candidate, quality, plan_path, plan_sha256):
    sources = {}
    plan = read_bound_json(plan_path, plan_sha256, sources)
    require(plan['lengths'] == LENGTHS and plan['repeats'] == 3,
            'only the frozen 18-request short diagnostic is supported')
    require(type(plan['frozen_at']) in (int, float) and
            math.isfinite(plan['frozen_at']), 'invalid plan freeze time')
    require(re.fullmatch('[0-9a-f]{64}', plan['runtime_binary_sha256']) and
            re.fullmatch('[0-9a-f]{40}', plan['runtime_source_commit']),
            'invalid runtime identity')
    if 'parent_plan_path' in plan:
        read_bound_json(plan['parent_plan_path'], plan['parent_plan_sha256'], sources)
    bound_file(plan['runtime_binary_path'], plan['runtime_binary_sha256'], sources)
    build = read_bound_json(plan['build_identity_path'],
                           plan['build_identity_sha256'], sources)
    require(build['source_commit'] == plan['runtime_source_commit'] and
            build['binary_sha256'] == plan['runtime_binary_sha256'] and
            build['binary'] == plan['runtime_binary_path'] and
            build['build_rc'] == 0 and build['warnings'] == 0 and
            build['runtime_tests_before_http'] is False,
            'build identity differs from frozen executable/source')
    binary_dir = Path(plan['runtime_binary_path']).parent
    for name, key in (('CMakeCache.txt', 'cmake_cache_sha256'),
                      ('compile_commands.json', 'compile_commands_sha256')):
        bound_file(binary_dir / name, build[key], sources)
    bound_file(Path(__file__), plan['tool_sha256'][Path(__file__).name], sources)
    require(len(plan['configuration_sha256']) == 3,
            'model config, index and hot list must be bound')
    for path, digest in plan['configuration_sha256'].items():
        bound_file(path, digest, sources)
    quality_reference = read_bound_json(plan['quality_reference_path'],
        plan['quality_reference_sha256'], sources)
    performance_reference = read_bound_json(plan['performance_reference_path'],
        plan['performance_reference_sha256'], sources)

    def read(directory, name):
        path = directory / name
        raw = path.read_bytes()
        sources[str(path.resolve())] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    def completed(directory, mode, state):
        directory = Path(directory).resolve()
        protocol = read(directory, 'protocol.json')
        wrapper = read(directory, 'wrapper-exit.json')
        result = read(directory, 'http/exit.json')
        command = read(directory, 'http/server-command.json')
        capacity = read(directory, 'http/capacity.json')
        expected_env = policy_environment(0, state, 'partition', True)
        require(protocol['mode'] == mode and protocol['policy_axis'] == 'partition'
                and protocol['partition'] == state and protocol['chunk_order'] == 0,
                'diagnostic mode or policy axis differs')
        require(protocol.get('phase_diagnostics') is True and
                protocol.get('diagnostic_scope') == DIAGNOSTIC_SCOPE and
                protocol.get('performance_acceptance') is False and
                protocol['effective_environment'] == expected_env,
                'explicit diagnostic protocol missing')
        require(command['effective_q4t_environment'] == expected_env and
                command.get('phase_diagnostics') is True and
                result.get('diagnostic_scope') == DIAGNOSTIC_SCOPE,
                'runtime diagnostic environment differs')
        require(protocol['binary_sha256'] == plan['runtime_binary_sha256'] and
                protocol['binary'] == plan['runtime_binary_path'],
                'runtime binary differs from frozen plan')
        for path, digest in plan['configuration_sha256'].items():
            require(protocol['input_config_sha256'].get(path) == digest,
                    'runtime configuration differs: ' + path)
        commit_path = directory / 'http/commit.txt'
        commit_raw = commit_path.read_bytes()
        sources[str(commit_path)] = hashlib.sha256(commit_raw).hexdigest()
        commit = commit_raw.decode().strip()
        require(commit == plan['runtime_source_commit'], 'runtime source differs')
        patch_path = directory / 'http/worktree.patch'
        require(patch_path.read_bytes() == b'', 'runtime source tree was dirty')
        sources[str(patch_path)] = hashlib.sha256(b'').hexdigest()
        identity_text = (directory / 'http/binary.sha256').read_text().strip()
        require(identity_text == plan['runtime_binary_sha256'],
                'HTTP runner executable identity differs')
        require(set(RUN_TOOLS + ('offload_policy.py',)).issubset(
                protocol['tool_sha256']), 'incomplete tool identity')
        for name, digest in protocol['tool_sha256'].items():
            require(plan['tool_sha256'].get(name) == digest,
                    'tool differs from frozen plan: ' + name)
            copied = directory / 'tools' / name
            require(hashlib.sha256(copied.read_bytes()).hexdigest() == digest,
                    'copied tool changed: ' + name)
            sources[str(copied)] = digest
        require(wrapper['runner_rc'] == wrapper['monitor_rc'] == result['server']
                == 0 and wrapper['failure'] is None and
                not wrapper['cleanup_failed'] and
                wrapper['unit_after_cleanup']['LoadState'] == 'not-found' and
                result['http_output_checks_passed'] is True and
                result['failure'] is None and result['cleanup_failure'] is None,
                'HTTP run failed or cleanup incomplete')
        require(result['performance_acceptance'] is False,
                'diagnostic data labelled performance acceptance')
        require(capacity['matches_requested'] is True and
                capacity['requested'] == capacity['effective'] == CAPACITY,
                'effective capacity differs')
        require(protocol['swap_max_bytes'] == 0, 'swap policy differs')
        group = read(directory, 'runner-process-group.json')
        require(group['cleanup_complete'] is True and
                group['runner_reaped'] is True and group['returncode'] == 0 and
                group['failure'] is None and not group['signals'] and
                not group['unexpected_live_descendants_after_runner_exit'] and
                group['after_cleanup']['absent'] is True and
                all(not group['after_cleanup'][key] for key in
                    ('live_pids', 'zombie_pids', 'errors')),
                'runner process group not fully reaped')
        require(type(group['runner_pid']) is int and group['runner_pid'] > 0 and
                group['runner_pid'] == group['pgid'] and
                group['timeout_s'] == protocol['runner_timeout_s'] and
                wrapper['started_t'] <= group['started_t'] < group['ended_t']
                <= wrapper['ended_t'], 'runner identity or timing differs')
        identity = read(directory, 'http/isolation/identity.json')
        props = identity['properties']
        charge = protocol['host_cache_max_bytes']
        expected_charge = str(charge) if charge else 'infinity'
        cgroup = '/system.slice/' + protocol['unit']
        require(type(identity['pid']) is int and identity['pid'] > 0 and
                int((directory / 'http/server.pid').read_text()) == identity['pid']
                == int(props['MainPID']) and props['ControlGroup'] == cgroup and
                '0::' + cgroup in identity['membership'] and
                props['MemoryMax'] == expected_charge and
                props['MemorySwapMax'] == '0' and
                props['MemoryAccounting'] == 'yes',
                'actual service PID/cgroup/charge differs')
        cleanup = read(directory, 'http/isolation/cleanup.json')
        require(cleanup['stop_rc'] == 0 and cleanup['unit_removed'] is True and
                cleanup['properties_after']['LoadState'] == 'not-found' and
                cleanup['properties_after']['MainPID'] == '0',
                'service cleanup not established')
        isolation = command['isolation']
        require(isolation['systemd_unit'] == protocol['unit'] and
                isolation['host_cache_max_bytes'] == charge and
                isolation['swap_max_bytes'] == 0, 'server isolation differs')
        argv = command['argv']
        require(argv[:2] == [protocol['binary'], 'serve'] and
                argv.count('--no-mtp') == 1 and '--mtp' not in argv,
                'ordinary no-MTP serving not established')
        for key, value in (('--moe-resident-slots', '256'),
                           ('--max-len', '262144'), ('--max-seq', '1'),
                           ('--max-prefill', '8192')):
            require(argv.count(key) == 1 and argv[argv.index(key) + 1] == value,
                    'server command differs: ' + key)
        records, runtime_log = read_phase_records(directory / 'http/server.log',
                                                  sources)
        require(runtime_log.splitlines().count(
            '[q4t][offload_diag_config] enabled=1 '
            'schema=q4t.offload_phase.v1') == 1,
            'diagnostic runtime activation missing or duplicate')
        path = partition_path_evidence(runtime_log, state)
        require(path['runtime_eligible'] is True,
                'partition path was not eligible in diagnostic execution')
        responses = []
        if mode == 'quality':
            responses = read(directory, 'http/quality/responses.json')
            old = {row['prompt_sha256']: row for row in quality_reference}
            require(len(responses) == len(old) == 11,
                    'fixed quality count differs')
            require(len({r['prompt_sha256'] for r in responses}) == 11,
                    'quality prompts duplicate')
            for row in responses:
                ref = old[row['prompt_sha256']]
                require(all(row[key] == ref[key] for key in
                        ('text', 'actual_input', 'actual_output', 'finish')),
                        'quality response differs from frozen reference')
        else:
            require(protocol['lengths'] == LENGTHS and protocol['repeats'] == 3
                    and protocol['host_cache_max_bytes'] == 16 << 30,
                    'short diagnostic schedule or memory charge differs')
            gate = read(directory, 'cache-gate.json')
            require(gate['cold_payload_established'] is True and
                    gate['payload_resident_bytes'] == 0 and
                    gate['advice_errors'] == [], 'initial payload cache not cold')
            refs = {r['length']: r for r in performance_reference}
            for length in LENGTHS:
                rows = read(directory, f'http/context-{length}/responses.json')
                require(len(rows) == 3, 'short diagnostic request count differs')
                ref = refs[length]
                require(len(set(ref['outputs'])) == 1, 'reference not deterministic')
                for row in rows:
                    require(row['actual_input'] == length and
                            row['actual_output'] == 256 and row['finish'] == ['length']
                            and row['prompt_sha256'] == ref['prompt_sha256'] and
                            hashlib.sha256(row['text'].encode()).hexdigest() ==
                            ref['outputs'][0], 'diagnostic HTTP output differs')
                responses.extend(rows)
        require(all(row['success'] and row['response_id_valid'] is True
                    for row in responses), 'failed HTTP or invalid response ID')
        ids = [row['response_id'] for row in responses]
        require(len(set(ids)) == len(ids) and set(ids) == set(records),
                'HTTP and phase records are not one-to-one')
        audited = [audit_record(records[row['response_id']], row)
                   for row in responses]
        return {'started_t': wrapper['started_t'], 'ended_t': wrapper['ended_t'],
                'request_count': len(responses), 'requests': audited,
                'protocol': protocol, 'runtime_path': path}

    groups = {'quality': completed(quality, 'quality', 1),
              'off': completed(baseline, 'performance', 0),
              'on': completed(candidate, 'performance', 1)}
    require(build['ended_t'] <= plan['frozen_at'] <=
            groups['quality']['started_t'] <=
            groups['quality']['ended_t'] <= groups['off']['started_t'] <=
            groups['off']['ended_t'] <= groups['on']['started_t'] <=
            groups['on']['ended_t'], 'quality -> off -> on order differs')
    return {'schema': 'q4t.offload_diagnostic_audit.v1',
            'decision': 'DIAGNOSTIC_EVIDENCE_COMPLETE',
            'diagnostic_scope': DIAGNOSTIC_SCOPE, 'groups': groups,
            'performance_acceptance': False, 'causal_claim': False,
            'prior_full_matrix_decision': 'NO_GO_FULL_PERFORMANCE_SCREEN',
            'total_physical_RAM': 'INDETERMINATE', 'source_sha256': sources,
            'limitations': ['Same binary with instrumentation; throughput is '
                'descriptive only and cannot supersede the prior NO_GO.',
                'Off then on, three repetitions per tier, inherited cache '
                'between tiers; no randomized or statistical causal inference.',
                'Only the first 1K request follows model loading; 4K/8K first '
                'requests inherit earlier tiers, not cold service requests.',
                'PID counters include all files; unknown I/O stays unknown.',
                'Phase deltas use actual server boundaries; shape-single/multi '
                'counters are not request prefill/decode categories.',
                'CUDA/RSS/model file cache overlap and instantaneous physical '
                'RAM are not established by this audit.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'candidate', 'quality', 'plan', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--plan-sha256', required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'refusing to overwrite diagnostic evidence')
    try:
        report = audit_evidence(args.baseline, args.candidate, args.quality,
                                args.plan, args.plan_sha256)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError,
            OSError) as error:
        report = {'schema': 'q4t.offload_diagnostic_audit.v1',
                  'decision': 'INVALID_DIAGNOSTIC_EVIDENCE',
                  'failure': f'{type(error).__name__}: {error}',
                  'performance_acceptance': False,
                  'total_physical_RAM': 'INDETERMINATE'}
    with args.output.open('x') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print(json.dumps({key: report[key] for key in
                     ('decision', 'performance_acceptance', 'total_physical_RAM')}))
    return int(report['decision'] != 'DIAGNOSTIC_EVIDENCE_COMPLETE')


if __name__ == '__main__':
    raise SystemExit(main())
