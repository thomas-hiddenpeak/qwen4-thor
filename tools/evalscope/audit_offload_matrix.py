"""Read-only audit of the frozen six-tier off/on experiment.

This grants only PASS_FULL_PERFORMANCE_SCREEN, an observed-range engineering
gate. It does not establish statistical noninferiority, numerical correctness,
business/lifecycle acceptance, total physical RAM compliance, or deployment.
No service, subprocess, cache advice, model payload read, or GPU call is made.
Both completion records are required before any request/monitor data is read.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

import compare_chunk_order as pilot
from offload_policy import (AXES, check_partition_plan, check_policy_protocol,
                            partition_path_evidence)

ROOT = Path(__file__).resolve().parents[2]
LENGTHS = [1024, 4096, 8192, 45056, 204800, 261887]
CAPACITY = pilot.CAPACITY
PLAN_SHA256 = 'be667395434ad109174bf14ebc9c536b88cadab9196cfbdd165b1fb00bd820aa'
TOOLS = ('run_budget_experiment.py', 'run_acceptance.py',
         'isolated_service.py', 'monitor_memory.py', 'file_cache.py',
         'resource_metrics.py', 'memory_accounting.py')
ENVIRONMENT = {'Q4T_MOE_L2_SLOTS': '16', 'Q4T_MOE_MIRROR_K': '8',
               'Q4T_MOE_MAX_OPEN_SHARDS': '200', 'Q4T_MOE_EVICT_WEIGHT': '0',
               'Q4T_MOE_PREAD_MERGE': '1', 'Q4T_MOE_INLINE_MISS_LIMIT': '1'}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def argument(argv, flag):
    require(isinstance(argv, list) and argv.count(flag) == 1,
            'missing/duplicate command flag: ' + flag)
    return argv[argv.index(flag) + 1]


def target(length):
    return 257 if length == 261887 else 256


class Evidence:
    """Hash every consumed source and reject changes during an audit."""
    def __init__(self):
        self.sources = {}

    def raw(self, path):
        path = Path(path).resolve()
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        require(str(path) not in self.sources or self.sources[str(path)] == digest,
                'source changed while auditing: ' + str(path))
        self.sources[str(path)] = digest
        return data

    def text(self, path):
        return self.raw(path).decode('utf-8')

    def json(self, path):
        return json.loads(self.raw(path))

    def verify(self, path, digest):
        self.raw(path)
        require(self.sources[str(Path(path).resolve())] == digest,
                'source SHA mismatch: ' + str(path))

    def recheck(self):
        for path, digest in tuple(self.sources.items()):
            self.verify(path, digest)


def reuse_pilot(evidence, decision_path, policy_axis='chunk-order',
                expected_plan_sha256=None):
    """Reuse the completed first-test quality and screening with SHA closure."""
    saved = evidence.json(decision_path)
    require(saved['decision'] == 'PASS_SCREENING' and
            saved['screening_passed'] is True and
            saved['evidence_contracts_passed'] is True and
            saved['quality_11_passed'] is True, 'pilot/quality precondition failed')
    require(bool(saved['source_sha256']), 'pilot has no source identities')
    for path, digest in saved['source_sha256'].items():
        evidence.verify(path, digest)
    base = decision_path.parent
    # The existing frozen pilot comparator independently rechecks its exact
    # 11 quality cases and three off/on observations; no inference is repeated.
    kwargs = {}
    if policy_axis == 'partition':
        require(saved.get('policy_axis') == 'partition' and
                saved['plan_sha256'] == expected_plan_sha256,
                'pilot belongs to another policy axis or frozen plan')
        kwargs = dict(policy_axis=policy_axis,
                      plan_path=Path(saved['frozen_contract']),
                      expected_plan_sha256=expected_plan_sha256,
                      numerical_evidence_sha256=saved['numerical_evidence_sha256'])
    else:
        require(saved.get('policy_axis', 'chunk-order') == 'chunk-order',
                'partition pilot cannot qualify a chunk-order matrix')
    recomputed = pilot.compare_evidence(base / 'pilot-off', base / 'pilot-on',
                                        base / 'quality-on', **kwargs)
    require(saved == recomputed, 'saved pilot decision differs from its evidence')
    protocols = [evidence.json(base / name / 'protocol.json')
                 for name in ('quality-on', 'pilot-off', 'pilot-on')]
    for name, protocol in zip(('quality-on', 'pilot-off', 'pilot-on'), protocols):
        require(evidence.text(base / name / 'http/binary.sha256').strip() ==
                protocol['binary_sha256'], 'reused binary identity mismatch')
    result = {'binary_sha256': protocols[0]['binary_sha256'],
            'tool_sha256': protocols[1]['tool_sha256'],
            'ended_t': evidence.json(base / 'pilot-on/wrapper-exit.json')['ended_t'],
            'decision_source': str(decision_path)}
    if policy_axis == 'partition':
        result['numerical'] = recomputed['numerical']
    return result


def screen_tier(baseline, candidate):
    require(len(baseline) == len(candidate) == 3, 'tier requires exactly 3 runs')
    for row in baseline + candidate:
        require(all(finite(row[k]) and row[k] > 0
                    for k in ('ttft', 'decode_tps')), 'invalid metric')
    bt, ct = ([r['ttft'] for r in rows] for rows in (baseline, candidate))
    bd, cd = ([r['decode_tps'] for r in rows] for rows in (baseline, candidate))
    checks = {'first_of_tier_ttft': ct[0] <= bt[0],
              'later_ttft': max(ct[1:]) <= max(bt[1:]),
              'decode': min(cd) >= min(bd)}
    return {'passed': all(checks.values()), 'checks': checks,
            'baseline': {'ttft_s': bt, 'decode_tps': bd},
            'candidate': {'ttft_s': ct, 'decode_tps': cd}}


def check_capacity(value):
    require(value['matches_requested'] is True and
            value['requested'] == value['effective'] ==
            value['reported_requested'] == CAPACITY and
            value['budget_enabled'] == '1' and
            value['budget_feasible'] == 'true', 'capacity/budget contract failed')


def check_completion(evidence, directory):
    wrapper = evidence.json(directory / 'wrapper-exit.json')
    http = evidence.json(directory / 'http/exit.json')
    require(wrapper['runner_rc'] == wrapper['monitor_rc'] == 0 and
            not wrapper['failure'] and wrapper['cleanup_failed'] is False and
            wrapper['unit_after_cleanup']['LoadState'] == 'not-found',
            'wrapper failed/unclean')
    require(http['server'] == 0 and http['http_output_checks_passed'] is True and
            not http['failure'] and not http['cleanup_failure'] and
            http['completed'] == 6, 'HTTP incomplete/failed')
    for record in (wrapper, http):
        require(record['full_offload_matrix_completed'] is True and
                record['partial_performance_matrix'] is False and
                record['performance_scope'] == 'six_tier',
                'partial results cannot qualify as full matrix')
    require(wrapper['partial_offload_matrix'] is False and
            http['full_five_tier_completed'] is True, 'missing full matrix flags')
    require(all(finite(wrapper[k]) for k in ('started_t', 'ended_t')) and
            wrapper['started_t'] < wrapper['ended_t'], 'invalid group times')
    return wrapper


def check_processes(evidence, directory, protocol, wrapper):
    group = evidence.json(directory / 'runner-process-group.json')
    require(group['cleanup_complete'] is True and group['runner_reaped'] is True
            and not group['failure'] and group['returncode'] == 0 and
            not group['unexpected_live_descendants_after_runner_exit'] and
            not group['signals'] and group['after_cleanup']['absent'] is True and
            all(not group['after_cleanup'][k]
                for k in ('live_pids', 'zombie_pids', 'errors')),
            'runner/client process group failed or was not reaped')
    require(type(group['runner_pid']) is int and group['runner_pid'] > 0 and
            group['runner_pid'] == group['pgid'] and
            group['timeout_s'] == protocol['runner_timeout_s'],
            'runner group identity/timeout mismatch')
    require(all(finite(group[k]) for k in ('started_t', 'ended_t')) and
            wrapper['started_t'] <= group['started_t'] < group['ended_t'] <=
            wrapper['ended_t'], 'runner time outside wrapper')
    identity = evidence.json(directory / 'http/isolation/identity.json')
    props = identity['properties']
    cgroup = '/system.slice/' + protocol['unit']
    require(type(identity['pid']) is int and identity['pid'] > 0 and
            int(evidence.text(directory / 'http/server.pid')) == identity['pid'] ==
            int(props['MainPID']) and props['ControlGroup'] == cgroup and
            '0::' + cgroup in identity['membership'] and
            int(props['MemoryMax']) == 16 << 30 and
            props['MemorySwapMax'] == '0' and props['MemoryAccounting'] == 'yes',
            'service PID/cgroup/limits identity mismatch')
    cleanup = evidence.json(directory / 'http/isolation/cleanup.json')
    require(cleanup['stop_rc'] == 0 and cleanup['unit_removed'] is True and
            cleanup['properties_after']['LoadState'] == 'not-found' and
            cleanup['properties_after']['MainPID'] == '0', 'service cleanup failed')
    # reset-failed can return 1 after a successfully removed unit. Unit absence
    # and server exit above are authoritative, not reset_rc in isolation.
    return group, identity


def check_monitor(evidence, directory, protocol, identity, wrapper):
    monitor = evidence.json(directory / 'monitor-command.json')
    for flag, expected in {
            '--model-dir': str(pilot.MODEL),
            '--pid-file': str(directory / 'http/server.pid'),
            '--phase-file': str(directory / 'http/memory-phase.txt'),
            '--cgroup-path': '/system.slice/' + protocol['unit'],
            '--ready-file': str(directory / 'memory/ready'),
            '--stop-file': str(directory / 'memory/stop'),
            '--out': str(directory / 'memory'),
            '--file-cache-mode': 'endpoints'}.items():
        require(argument(monitor, flag) == expected, 'monitor wiring: ' + flag)
    require(float(argument(monitor, '--interval')) == 1 and
            float(argument(monitor, '--gpu-interval')) == 10,
            'monitor cadence differs')
    peak = evidence.json(directory / 'memory/memory-peak.json')
    require(peak['root_pid'] == identity['pid'] and
            type(peak['root_start_ticks']) is int and peak['root_start_ticks'] > 0 and
            peak['sampling_complete'] is True and
            peak['stop_reason'] == 'target_exited' and
            peak['prelaunch_sample_present'] is True and
            not peak['existing_named_pids_at_start'] and
            peak['target_sample_count'] > 0, 'monitor identity/coverage incomplete')
    policy = peak['observation_policy']
    require(policy['resource_interval_seconds'] == 1 and
            policy['gpu_interval_seconds'] == 10 and
            policy['file_cache_mode'] == 'endpoints' and
            policy['carry_forward_values'] is False, 'monitor policy differs')
    endpoints = peak['model_file_cache_endpoint_observations']
    require(len(endpoints) == 2 and
            [r['phase'] for r in endpoints] == ['before_start', 'after_exit'] and
            all(r['model_file_cache_status'] == 'ok' for r in endpoints) and
            all(finite(r['t']) for r in endpoints) and
            wrapper['started_t'] <= endpoints[0]['t'] < endpoints[1]['t'] <=
            wrapper['ended_t'], 'monitor endpoints missing or out of sequence')
    resource = peak['resource_observations']
    require(resource['binding_mismatch_samples'] == 0 and
            resource['cgroup_paths'] == ['/sys/fs/cgroup/system.slice/' + protocol['unit']],
            'monitor bound to another service')
    # These are coverage/identity checks only. Resource sidecar counter deltas,
    # OOM, physical overlap and sampling gaps require their own resource audit.
    return {'sampling_complete': True, 'root_pid': identity['pid'],
            'resource_acceptance': 'SEPARATE_AUDIT_REQUIRED',
            'total_physical_RAM': 'INDETERMINATE',
            'file_cache_during_requests': 'NOT_SAMPLED'}


def check_request(evidence, directory, length, index, row, boundary,
                  capacity, group, previous_end, prompt, tokens, port):
    require(type(row['success']) is int and row['success'] == 1 and
            type(row['actual_input']) is int and row['actual_input'] == length and
            type(row['actual_output']) is int and
            row['actual_output'] == row['requested_max_tokens'] == tokens and
            row['finish'] == ['length'] and row['request_stream'] is True and
            row['requested_capacity'] == row['effective_capacity'] == CAPACITY and
            length + tokens <= CAPACITY['max_len'], 'raw response contract failed')
    require(all(finite(row[k]) for k in ('ttft', 'latency')) and
            row['latency'] > row['ttft'] > 0 and isinstance(row['text'], str),
            'invalid raw request metrics/text')
    require(row['prompt_sha256'] == hashlib.sha256(prompt.encode()).hexdigest(),
            'response prompt differs from frozen fixture')
    case = directory / f'http/context-{length}'
    request = case / f'run-{index + 1}'
    wire = [json.loads(line) for line in evidence.text(request / 'requests.jsonl').splitlines()]
    require(wire == [{'prompt': prompt}], 'per-client prompt file differs')
    require(evidence.text(case / f'output-{index}.txt') == row['text'],
            'raw output text differs from response')
    client = evidence.json(request / 'client-exit.json')
    before, after = client['before'], client['after']
    label = f'context-{length}:run{index + 1}'
    require(client['returncode'] == after['client_returncode'] == 0 and
            before['event'] == 'client_before' and after['event'] == 'client_after' and
            before['request_group'] == after['request_group'] == label and
            before['case'] == after['case'] == f'context-{length}' and
            before['expected_requests'] == 1 and before['requested_output'] == tokens and
            before['expected_input_min'] == before['expected_input_max'] == length and
            before['capacity'] == capacity, 'client exit/identity contract failed')
    require(boundary['request_index'] == index + 1 and
            boundary['request_group'] == label and boundary['client_before'] == before and
            boundary['client_after'] == after and boundary['http_timing'] == row['timing'] and
            boundary['capacity'] == capacity and all(boundary[k] == row[k] for k in
                ('success', 'actual_input', 'actual_output', 'finish', 'requested_max_tokens')),
            'request boundaries differ from raw evidence')
    timing = row['timing']
    values = [before['monotonic_seconds'], timing['start_monotonic_seconds'],
              timing['end_monotonic_seconds'], after['monotonic_seconds']]
    require(all(finite(v) for v in values) and values == sorted(values) and
            previous_end <= values[0] and timing['within_client_boundaries'] is True and
            values[1] < values[2] and math.isclose(values[2] - values[1], row['latency'],
                                                  rel_tol=1e-9, abs_tol=1e-6),
            'HTTP timing/order does not match client boundaries')
    require(all(finite(v) for v in (before['unix_seconds'], after['unix_seconds'])) and
            group['started_t'] <= before['unix_seconds'] < after['unix_seconds'] <=
            group['ended_t'], 'client outside runner group time')
    command = evidence.json(request / 'command.json')
    for flag, value in {'--model': 'qwen3.8-flash-next', '--api': 'openai',
                        '--url': f'http://127.0.0.1:{port}/v1/chat/completions',
                        '--tokenizer-path': str(pilot.MODEL),
                        '--dataset': 'line_by_line', '--dataset-path': str(request / 'requests.jsonl'),
                        '--min-prompt-length': str(length), '--max-prompt-length': str(length),
                        '--max-tokens': str(tokens), '--temperature': '0', '--seed': '20260920',
                        '--parallel': '1', '--number': '1', '--warmup-num': '0'}.items():
        require(argument(command, flag) == value, 'client command differs: ' + flag)
    require(all(command.count(flag) == 1 for flag in
                ('--stream', '--no-test-connection', '--no-apply-chat-template')) and
            '--no-stream' not in command, 'stream/template/probe contract differs')
    return values[-1]


def check_run(evidence, directory, order, plan, wrapper, reference, reference_sha,
              policy_axis='chunk-order'):
    p = evidence.json(directory / 'protocol.json')
    check_policy_protocol(p, order, policy_axis)
    require(p['mode'] == 'performance' and
            p['lengths'] == LENGTHS and p['repeats'] == 3 and
            p['binary_sha256'] == plan['runtime_binary_sha256'],
            'protocol binary/order/tiers differ from frozen matrix')
    require(p['host_cache_max_bytes'] == 16 << 30 and p['swap_max_bytes'] == 0 and
            p['cold_payload_max_resident_bytes'] == 0 and
            p['runner_timeout_s'] == plan['runner_timeout_s'] and
            p['request_deadline_ms'] == plan['request_deadline_ms'] and
            {key: p[key] for key in CAPACITY} == CAPACITY,
            'protocol resource/capacity/deadline mismatch')
    require(p['monitor'] == {'interval_seconds': 1.0, 'gpu_interval_seconds': 10.0,
                            'file_cache_mode': 'endpoints',
                            'live_file_cache_observation': 'NOT_SAMPLED'},
            'protocol monitoring differs')
    require(p['output_tokens_by_length'] == {str(n): target(n) for n in LENGTHS} and
            p['output_tokens'] is None, 'target tier must request 257 output tokens')
    server = evidence.json(directory / 'http/server-command.json')
    capacity = evidence.json(directory / 'http/capacity.json')
    check_capacity(capacity)
    require(server['effective_q4t_environment'] == p['effective_environment'] and
            server['hot_list'] == {'path': str(pilot.HOT),
                'sha256': pilot.FROZEN_CONFIG_SHA256[str(pilot.HOT)]} and
            server['isolation']['systemd_unit'] == p['unit'] and
            server['isolation']['host_cache_max_bytes'] == 16 << 30 and
            server['isolation']['swap_max_bytes'] == 0, 'actual server configuration differs')
    argv = server['argv']
    require(argv[:2] == [p['binary'], 'serve'] and argv.count('--no-mtp') == 1 and
            '--mtp' not in argv and '--no-budget' not in argv, 'server binary/MTP/budget differs')
    for flag, value in {'--model-dir': str(pilot.MODEL), '--moe-hot-list': str(pilot.HOT),
                        '--moe-resident-slots': '256', '--max-len': '262144',
                        '--max-seq': '1', '--max-prefill': '8192',
                        '--request-deadline-ms': '1800000'}.items():
        require(argument(argv, flag) == value, 'server command differs: ' + flag)
    port = argument(argv, '--port')
    require(0 < int(port) < 65536, 'invalid server port')
    require(evidence.text(directory / 'http/binary.sha256').strip() == p['binary_sha256'],
            'recorded HTTP binary differs')
    # Check the built artifact and its adjacent actual build cache, rather than
    # any CMakeCache in the source tree or another build directory.
    evidence.verify(Path(p['binary']), p['binary_sha256'])
    build_cache = Path(p['binary']).parent / 'CMakeCache.txt'
    evidence.raw(build_cache)
    evidence.verify(directory / 'http/CMakeCache.txt',
                    evidence.sources[str(build_cache.resolve())])
    expected_tools = [set(TOOLS) | {'offload_policy.py'}]
    if policy_axis == 'chunk-order':
        expected_tools.append(set(TOOLS))  # Historical seven-file runner only.
    require(set(p['tool_sha256']) in expected_tools,
            'incomplete tool source identities')
    if policy_axis == 'partition':
        require(p['tool_sha256'] == plan['tool_sha256'],
                'partition tools differ from frozen plan')
    for name, digest in p['tool_sha256'].items():
        evidence.verify(directory / 'tools' / name, digest)
    evidence.verify(directory / 'http/run_acceptance.py', p['tool_sha256']['run_acceptance.py'])
    for path, digest in pilot.FROZEN_CONFIG_SHA256.items():
        require(p['input_config_sha256'].get(path) == digest, 'model/hot identity differs')
        evidence.verify(path, digest)
    command = evidence.json(directory / 'runner-command.json')
    for flag, value in {'--reference': str(reference), '--perf-lengths': ','.join(map(str, LENGTHS)),
                        '--perf-repeats': '3', '--extra-lengths': '261887',
                        '--target-total': '262144', '--systemd-unit': p['unit'],
                        '--binary': p['binary'], '--fixtures': str(pilot.PERF_FIXTURES),
                        '--output': str(directory / 'http')}.items():
        require(argument(command, flag) == value, 'runner command differs: ' + flag)
    require(p['input_config_sha256'].get(str(reference)) == reference_sha,
            'reference chain SHA differs')
    evidence.verify(reference, reference_sha)
    old = evidence.json(reference)
    require([r['length'] for r in old] == LENGTHS, 'reference is missing a tier')
    group, identity = check_processes(evidence, directory, p, wrapper)
    monitor = check_monitor(evidence, directory, p, identity, wrapper)
    peak = evidence.json(directory / 'memory/memory-peak.json')
    require(peak['model_file_cache_endpoint_observations'][0]['t'] <= group['started_t'],
            'prelaunch monitoring started after the runner')
    gate = evidence.json(directory / 'cache-gate.json')
    require(gate['cold_payload_established'] is True and gate['payload_resident_bytes'] == 0
            and not gate['advice_errors'], 'cold payload zero not established')
    execution = evidence.json(directory / 'http/performance-plan.json')
    expected_requests = [{'input_tokens': n, 'max_tokens': target(n),
                          'total_tokens': n + target(n), 'repeats': 3} for n in LENGTHS]
    require(execution['requests'] == expected_requests and execution['lengths'] == LENGTHS and
            execution['repeats'] == 3 and execution['scope'] == 'six_tier' and
            execution['partial'] is False and execution['full_offload_matrix_requested'] is True and
            execution['client_protocol'] == 'one_evalscope_process_per_request' and
            {k: v for k, v in p['performance_plan'].items() if k != 'partial_offload_matrix'} == execution
            and p['performance_plan']['partial_offload_matrix'] is False,
            'actual performance plan differs')
    results = evidence.json(directory / 'http/results.json')
    require([r['length'] for r in results] == LENGTHS, 'missing/duplicate/out-of-order tier')
    require(p['fixtures'] == str(pilot.PERF_FIXTURES) and
            set(p['fixture_sha256']) == {f'context-{n}/requests.jsonl' for n in LENGTHS},
            'fixture selection differs')
    previous_end = -math.inf
    rows_by_tier = []
    for n, summary, prior in zip(LENGTHS, results, old):
        relative = f'context-{n}/requests.jsonl'
        fixture = pilot.PERF_FIXTURES / relative
        digest = p['fixture_sha256'][relative]
        require(p['input_config_sha256'].get(str(fixture)) == digest,
                'fixture hash differs from config identity')
        evidence.verify(fixture, digest)
        first = evidence.text(fixture).splitlines()[0]
        prompt = json.loads(first)['prompt']
        require(isinstance(prompt, str), 'fixture prompt is not text')
        repeated = (first + '\n') * 3
        require(evidence.text(directory / f'http/inputs/context-{n}.jsonl') == repeated and
                evidence.text(directory / f'http/context-{n}/requests.jsonl') == repeated,
                'prepared repeated prompt differs from source fixture')
        rows = evidence.json(directory / f'http/context-{n}/responses.json')
        boundaries = evidence.json(directory / f'http/context-{n}/request-boundaries.json')
        require(len(rows) == len(boundaries) == 3, 'missing/extra raw requests')
        for i, (row, boundary) in enumerate(zip(rows, boundaries)):
            previous_end = check_request(evidence, directory, n, i, row, boundary,
                                         capacity, group, previous_end, prompt, target(n), port)
        hashes = [hashlib.sha256(r['text'].encode()).hexdigest() for r in rows]
        metrics = [{'ttft': r['ttft'], 'decode_tps':
                    (target(n) - 1) / (r['latency'] - r['ttft'])} for r in rows]
        prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
        require(len(set(hashes)) == 1 and summary['outputs'] == hashes and
                summary['deterministic'] is True and summary['prompt_sha256'] == prompt_sha and
                summary['metrics'] == metrics and summary['performance_scope'] == 'six_tier' and
                summary['partial_performance_matrix'] is False,
                'summary does not match raw response/metric evidence')
        require(prior['prompt_sha256'] == prompt_sha and len(prior['outputs']) == 3 and
                prior['outputs'] == hashes, 'text/prompt differs from declared reference')
        rows_by_tier.append(rows)
    require(boundaries[-1]['client_after']['unix_seconds'] <=
            peak['model_file_cache_endpoint_observations'][1]['t'],
            'monitor after-exit endpoint predates the last client')
    runtime_path = None
    if policy_axis == 'partition':
        runtime_path = partition_path_evidence(
            evidence.text(directory / 'http/server.log'), order)
    return {'protocol': p, 'wrapper': wrapper, 'group': group, 'identity': identity,
            'monitor': monitor, 'results': results, 'rows': rows_by_tier,
            'runtime_path': runtime_path,
            'build_cache_sha256': evidence.sources[str(build_cache.resolve())],
            'evalscope_version': evidence.text(directory / 'http/evalscope-version.txt')}


def audit_matrix(plan_path, baseline, candidate, pilot_decision, *,
                 policy_axis='chunk-order', expected_plan_sha256=None):
    evidence = Evidence()
    result = {'schema': 1, 'decision': 'INVALID_EVIDENCE',
              'full_performance_screen_passed': False, 'performance_acceptance': False,
              'statistical_noninferiority': False, 'deployment_acceptance': False,
              'resource_acceptance': 'SEPARATE_AUDIT_REQUIRED',
              'numerical_acceptance': 'SEPARATE_AUDIT_REQUIRED',
              'business_acceptance': 'SEPARATE_AUDIT_REQUIRED',
              'lifecycle_acceptance': 'SEPARATE_AUDIT_REQUIRED',
              'total_physical_RAM': 'INDETERMINATE',
              'inference_limit': 'Observed ranges only; independent counter peaks are not added. '
                'Only the first 1024 request of each group follows payload0. Later requests '
                'inherit state, including the first request of every later tier.'}
    try:
        plan_path, baseline, candidate, pilot_decision = (
            Path(p).resolve() for p in (plan_path, baseline, candidate, pilot_decision))
        evidence.raw(Path(__file__))
        evidence.raw(Path(pilot.__file__))
        evidence.raw(Path(__file__).with_name('test_offload_matrix_audit.py'))
        require(policy_axis in AXES, 'unknown policy axis')
        if policy_axis == 'partition':
            require(isinstance(expected_plan_sha256, str) and
                    len(expected_plan_sha256) == 64,
                    'partition matrix requires explicit frozen plan SHA256')
            evidence.verify(plan_path, expected_plan_sha256)
        else:
            require(expected_plan_sha256 is None,
                    'chunk-order matrix uses its original frozen plan SHA256')
            evidence.verify(plan_path, PLAN_SHA256)
        plan = evidence.json(plan_path)
        if policy_axis == 'partition':
            check_partition_plan(plan)
            evidence.raw(Path(__file__).with_name('offload_policy.py'))
        else:
            require(plan.get('policy_axis', 'chunk-order') == 'chunk-order',
                    'wrong matrix policy axis')
        require(plan['lengths'] == LENGTHS and plan['repeats'] == 3 and
                plan['output_tokens'] == {'default': 256, '261887': 257}, 'wrong frozen plan')
        require(baseline != candidate, 'off/on require independent evidence directories')
        # Fail closed before touching in-progress responses or monitor sidecars.
        bx, cx = (check_completion(evidence, path) for path in (baseline, candidate))
        reused = (reuse_pilot(evidence, pilot_decision, policy_axis,
                              expected_plan_sha256)
                  if policy_axis == 'partition' else
                  reuse_pilot(evidence, pilot_decision))
        require(reused['binary_sha256'] == plan['runtime_binary_sha256'],
                'pilot quality used another binary')
        require(plan['frozen_at'] <= bx['started_t'] and
                reused['ended_t'] <= bx['started_t'] and bx['ended_t'] <= cx['started_t'],
                'pilot -> complete off -> on ordering not established')
        base = check_run(evidence, baseline, 0, plan, bx,
                         pilot.PERF_REFERENCE, pilot.PERF_REFERENCE_SHA256,
                         policy_axis)
        candidate_run = check_run(evidence, candidate, 1, plan, cx,
                                  baseline / 'http/results.json',
                                  evidence.sources[str(baseline / 'http/results.json')],
                                  policy_axis)
        bp, cp = base['protocol'], candidate_run['protocol']
        for key in ('binary', 'binary_sha256', 'tool_sha256', 'fixture_sha256', 'model_files',
                    'monitor', 'lengths', 'repeats', 'host_cache_max_bytes', 'swap_max_bytes',
                    'max_len', 'max_seq', 'max_prefill', 'request_deadline_ms', 'startup_timeout_s',
                    'runner_timeout_s', 'client_lifecycle', 'cache_protocol', 'performance_plan'):
            require(bp[key] == cp[key], 'unfair pair: ' + key)
        require(bp['tool_sha256'] == reused['tool_sha256'], 'tools changed after the pilot')
        require({k: v for k, v in bp['input_config_sha256'].items() if k != str(pilot.PERF_REFERENCE)} ==
                {k: v for k, v in cp['input_config_sha256'].items()
                 if k != str(baseline / 'http/results.json')},
                'configuration differs beyond the declared reference chain')
        require(bp['unit'] != cp['unit'] and
                base['group']['pgid'] != candidate_run['group']['pgid'] and
                base['identity']['pid'] != candidate_run['identity']['pid'],
                'off/on reused service or runner identity')
        require(base['evalscope_version'] == candidate_run['evalscope_version'],
                'evalscope version differs')
        require(base['build_cache_sha256'] == candidate_run['build_cache_sha256'],
                'actual build cache differs')
        require(all(a['text'] == b['text'] and a['prompt_sha256'] == b['prompt_sha256']
                    for br, cr in zip(base['rows'], candidate_run['rows'])
                    for a, b in zip(br, cr)), 'off/on prompt or exact output differs')
        tiers = [{'length': n, 'output_tokens': target(n),
                  **screen_tier(b['metrics'], c['metrics'])}
                 for n, b, c in zip(LENGTHS, base['results'], candidate_run['results'])]
        passed = all(t['passed'] for t in tiers)
        if policy_axis == 'partition':
            eligible = all(run['runtime_path']['runtime_eligible']
                           for run in (base, candidate_run))
            passed = passed and eligible
            result.update(policy_axis=policy_axis,
                          plan_sha256=expected_plan_sha256,
                          numerical_acceptance='PASS_FROZEN_BOUNDED_CONTRACTS',
                          numerical=reused['numerical'],
                          runtime_eligibility_passed=eligible,
                          runtime_paths={'off': base['runtime_path'],
                                         'on': candidate_run['runtime_path']})
        result.update(decision='PASS_FULL_PERFORMANCE_SCREEN' if passed else
                      'NO_GO_FULL_PERFORMANCE_SCREEN',
                      full_performance_screen_passed=passed,
                      evidence_contracts_passed=True, tiers=tiers,
                      reused_pilot_quality_11_passed=True, reused_pilot_screen_passed=True,
                      pilot=reused, baseline=str(baseline), candidate=str(candidate),
                      monitoring={'off': base['monitor'], 'on': candidate_run['monitor']})
        evidence.recheck()
    except (ValueError, KeyError, OSError, TypeError, IndexError, OverflowError) as error:
        result.update(decision='INVALID_EVIDENCE', full_performance_screen_passed=False,
                      evidence_contracts_passed=False,
                      failure=type(error).__name__ + ': ' + str(error))
    result['source_sha256'] = dict(sorted(evidence.sources.items()))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'baseline', 'candidate', 'pilot-decision', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--policy-axis', choices=AXES, default='chunk-order')
    parser.add_argument('--expected-plan-sha256')
    args = parser.parse_args()
    output = args.output.resolve()
    if not any(output.is_relative_to(ROOT / part) for part in ('build', '.q4t-work')):
        parser.error('output must be under build/ or .q4t-work/')
    # Reserve first: an existing report can never be overwritten, even on error.
    with output.open('x') as stream:
        report = audit_matrix(args.plan, args.baseline, args.candidate,
            args.pilot_decision, policy_axis=args.policy_axis,
            expected_plan_sha256=args.expected_plan_sha256)
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')
    print(report['decision'])
    return (0 if report['full_performance_screen_passed'] else
            2 if report['decision'] == 'INVALID_EVIDENCE' else 1)


if __name__ == '__main__':
    raise SystemExit(main())
