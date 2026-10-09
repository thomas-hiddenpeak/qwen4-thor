"""Audit frozen T4 verify-MoE HTTP evidence; never launch inference.

Only trusted local evalscope SQLite/pickle artifacts are read. Census is
complete; CUDA timing is a fixed sample, not an estimate of all-layer cost.
Host nanoseconds, stream elapsed milliseconds and client seconds are separate.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics

from analyze_mtp_init_timing import (
    LENGTHS, Reader, compare_runs, decode_json, load_run, number, require, ulp32)
from analyze_mtp_cycle_timing import (
    add_client_ranges, check_bindings, identity_gate, sampled_memory)

PREFIX = '[q4t][mtp_verify_moe_timing] '
POLICY = 't4_moe_16_calls_v1'
SAMPLE_STEPS = (0, 1, 32, 63)
SAMPLE_LAYERS = (0, 15, 32, 47)
SAMPLE_ORDINALS = (0, 5, 10, 15)
STEP_MARKS = ('engine_begin', 'draft_end', 'verify_readback_end',
              'accept_end', 'extend_readback_end', 'engine_end')
PHASES = ('draft', 'verify', 'accept', 'extend', 'finalize')
HOST_MARKS = ('moe_begin', 'routed_begin', 'counts_wait_begin',
              'counts_wait_end', 'offsets_end', 'expert_loop_begin',
              'expert_loop_end', 'routed_end', 'moe_end')
GPU_MARKS = ('moe_begin', 'router_end', 'topk_zero_end', 'list_end',
             'counts_copy_end', 'experts_joined', 'routed_end',
             'shared_gu_end', 'shared_act_end', 'shared_dn_end', 'moe_end')
GPU_INTERVALS = ('router', 'topk_zero', 'token_list', 'counts_copy',
                 'parallel_window', 'routed_finish', 'shared_gu',
                 'shared_activation', 'shared_dn', 'combine')
EXPERT_MARKS = ('gather_begin', 'gather_end', 'gu_end', 'activation_end', 'dn_end')
EXPERT_INTERVALS = ('gather_quant', 'gu', 'swiglu_quant', 'dn')
PRIMARY_SHA = '21d85a1705951d46752bbc1646a95ed5215489655272a19ec4b9ef2bce26f3b6'
DEVELOPMENT_SHA = '962b563232b27301ded1478ab89840d2e992d7e60199b3570bf9eb04e863cf99'
IMPORT_SOURCES = ('analyze_mtp_verify_moe_timing.py',
                  'test_mtp_verify_moe_timing.py', 'analyze_mtp_init_timing.py',
                  'analyze_mtp_cycle_timing.py', 'acceptance_mode.py',
                  'response_identity.py')


def integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum,
            label + ': expected integer >= ' + str(minimum))
    return value


def equal(record, key, expected):
    value = record.get(key)
    require(value == expected and type(value) is type(expected),
            f'{key}: expected {expected!r}, got {value!r}')


def object_value(value, label):
    require(isinstance(value, dict), label + ': expected object')
    return value


def named_marks(values, names, label, gpu=False):
    require(isinstance(values, list) and len(values) == len(names),
            label + ': marker count')
    result, previous = {}, 0
    for value, name in zip(values, names):
        object_value(value, label)
        equal(value, 'name', name)
        at = integer(value.get('at_ns'), label + '/' + name)
        require(at >= previous, label + ': host marker order')
        if gpu:
            equal(value, 'recorded', True)
            equal(value, 'ready', True)
        result[name], previous = at, at
    return result


def host_partition(timeline, names, label):
    values = [timeline[name] for name in names]
    parts = [b - a for a, b in zip(values, values[1:])]
    require(all(value >= 0 for value in parts) and
            sum(parts) == values[-1] - values[0], label + ': host closure')
    return parts


def gpu_closure(leaves, total):
    """Frozen arithmetic screen, not GPU accuracy or model tolerance.

    CUDA returns nonnegative binary32 intervals. Every leaf and the total
    contribute one full binary32 ULP; each binary64 partial sum contributes
    one full binary64 ULP. No empirical percent/absolute allowance is fitted.
    The shared-boundary equation is checked independently for each same-stream
    chain. Overlapping expert chains are never added to the main-stream total.
    """
    budget = ulp32(total)
    partial = 0.0
    for leaf in leaves:
        budget += ulp32(leaf)
        partial += leaf
        budget += math.ulp(partial)
    residual = total - partial
    require(abs(residual) <= budget, 'GPU shared-boundary closure')
    return {'leaf_sum_ms': partial, 'total_ms': total,
            'residual_ms': residual, 'budget_ms': budget}


def gpu_chain(raw, mark_names, interval_names, label):
    marks = named_marks(raw.get('gpu_marks'), mark_names, label, True)
    intervals = raw.get('gpu_intervals')
    require(isinstance(intervals, list) and len(intervals) == len(interval_names),
            label + ': GPU interval count')
    durations = {}
    for index, (item, name) in enumerate(zip(intervals, interval_names)):
        object_value(item, label)
        equal(item, 'name', name)
        equal(item, 'begin', mark_names[index])
        equal(item, 'end', mark_names[index + 1])
        durations[name] = number(item.get('stream_ms'), label + '/' + name)
    total = number(raw.get('stream_total_ms'), label + '/total')
    closure = gpu_closure(list(durations.values()), total)
    return {'record_host_ns': marks, 'interval_ms': durations,
            'stream_total_ms': total, 'closure': closure}


def records(log):
    result = []
    for line in log.splitlines():
        if '[q4t][mtp_verify_moe_timing]' not in line:
            continue
        require(line.startswith(PREFIX), 'malformed verify-MoE prefix')
        result.append(object_value(decode_json(line[len(PREFIX):]), 'trace'))
    return result


def census_row(row, layer, sampled):
    object_value(row, 'census')
    for key, value in [('layer', layer), ('rows', 4), ('experts', 512),
                       ('top_k', 10), ('routes', 40), ('invalid_counts', 0),
                       ('streams', 4), ('sampled', sampled)]:
        equal(row, key, value)
    hist = row.get('histogram')
    require(isinstance(hist, list) and len(hist) == 5, 'census histogram size')
    for value in hist:
        integer(value, 'histogram')
    active = integer(row.get('active_experts'), 'active experts')
    require(sum(hist) == 512 and sum(i * h for i, h in enumerate(hist)) == 40,
            'census expert/route conservation')
    require(active == sum(hist[1:]) and 10 <= active <= 40,
            'census active experts')
    return hist


def validate_sample(raw, census, step_marks):
    object_value(raw, 'sample')
    equal(raw, 'rows', 4)
    equal(raw, 'streams', 4)
    host = named_marks(raw.get('host_marks'), HOST_MARKS, 'sample host')
    host_partition(host, HOST_MARKS, 'sample host')
    require(step_marks['draft_end'] <= host['moe_begin'] <= host['moe_end'] <=
            step_marks['verify_readback_end'], 'MoE outside verify host window')
    counts = raw.get('counts')
    require(isinstance(counts, list) and len(counts) == census['active_experts'],
            'sample sparse counts coverage')
    hist, previous_id = [512 - len(counts), 0, 0, 0, 0], -1
    for item in counts:
        object_value(item, 'sparse count')
        expert = integer(item.get('expert_id'), 'expert ID')
        rows = integer(item.get('rows'), 'expert rows', 1)
        require(previous_id < expert < 512 and rows <= 4,
                'sparse counts order/shape')
        hist[rows] += 1
        previous_id = expert
    require(hist == census['histogram'], 'sample counts differ from full census')
    gpu = gpu_chain(raw, GPU_MARKS, GPU_INTERVALS, 'sample GPU')
    gm = gpu['record_host_ns']
    require(host['moe_begin'] <= gm['moe_begin'] <= gm['moe_end'] <= host['moe_end'],
            'GPU record timestamps outside MoE host window')
    require(host['counts_wait_begin'] <= gm['counts_copy_end'] <=
            host['counts_wait_end'], 'counts-copy event not inside existing wait')
    require(host['expert_loop_end'] <= gm['experts_joined'] <= host['routed_end'],
            'join event outside post-loop routed window')
    experts = raw.get('experts')
    require(isinstance(experts, list) and len(experts) == 4,
            'expert ordinal coverage')
    selected, absent, recorded = [], [], 11
    for expert, ordinal in zip(experts, SAMPLE_ORDINALS):
        object_value(expert, 'expert sample')
        equal(expert, 'ordinal', ordinal)
        present = ordinal < len(counts)
        equal(expert, 'present', present)
        if not present:
            for key, value in [('expert_id', -1), ('rows', 0), ('stream_index', -1),
                               ('gpu_marks', []), ('gpu_intervals', []),
                               ('stream_total_ms', None)]:
                equal(expert, key, value)
            absent.append(ordinal)
            continue
        equal(expert, 'expert_id', counts[ordinal]['expert_id'])
        equal(expert, 'rows', counts[ordinal]['rows'])
        equal(expert, 'stream_index', ordinal % 4)
        chain = gpu_chain(expert, EXPERT_MARKS, EXPERT_INTERVALS, 'expert GPU')
        em = chain['record_host_ns']
        require(host['expert_loop_begin'] <= em['gather_begin'] <=
                em['dn_end'] <= host['expert_loop_end'],
                'expert event record outside host expert loop')
        selected.append({'ordinal': ordinal, 'expert_id': expert['expert_id'],
                         'rows': expert['rows'], 'stream_index': ordinal % 4,
                         **chain})
        recorded += 5
    return {'step': raw['step'], 'layer': raw['layer'], 'host_ns': host,
            'host_moe_ns': host['moe_end'] - host['moe_begin'],
            'host_routed_ns': host['routed_end'] - host['routed_begin'],
            'host_counts_wait_ns': host['counts_wait_end'] - host['counts_wait_begin'],
            'host_expert_loop_ns': host['expert_loop_end'] - host['expert_loop_begin'],
            'census_histogram': hist, 'sparse_counts': counts, **gpu,
            'expert_samples': selected, 'absent_ordinals': absent,
            'event_recorded': recorded, 'experts_added_to_main_total': False}


def validate_trace(raw, response):
    object_value(raw, 'trace')
    for key, value in [('schema_version', 1), ('policy', POLICY),
                       ('response_id', response['response_id']),
                       ('prompt_tokens', response['actual_input']),
                       ('max_tokens', response['actual_output']), ('k', 3),
                       ('max_seq', 1), ('text_only', True), ('known_positions', True),
                       ('diagnostic_only', True), ('supported', True),
                       ('valid', True), ('complete', True), ('error', ''),
                       ('success', True), ('plain_tail', False),
                       ('finish_reason', 'length'), ('fallback', 'none'),
                       ('host_unit', 'ns'), ('gpu_unit', 'ms'),
                       ('stream_time_is_gpu_active', False),
                       ('host_and_stream_times_additive', False),
                       ('sample_steps', list(SAMPLE_STEPS)),
                       ('sample_layers', list(SAMPLE_LAYERS)),
                       ('sample_ordinals', list(SAMPLE_ORDINALS)),
                       ('event_capacity', 496), ('event_created', 496),
                       ('generated', response['actual_output'])]:
        equal(raw, key, value)
    require(response['finish'] == ['length'], 'trace requires length finish')
    for field in ('sample_steps', 'sample_layers', 'sample_ordinals'):
        require(all(type(value) is int for value in raw[field]),
                'sample policy entries must be integers')
    maximum = integer(raw['max_tokens'], 'max_tokens', 1)
    steps_n = integer(raw.get('step_count'), 'step_count', 1)
    equal(raw, 'step_capacity', min(maximum, 1024))
    equal(raw, 'mtp_steps', int(response['actual_path']['mtp_steps']))
    equal(raw, 'step_count', raw['mtp_steps'])
    equal(raw, 'layer_count', steps_n * 48)
    require(steps_n <= raw['step_capacity'], 'step capacity exceeded')
    setup = integer(raw.get('setup_host_ns'), 'setup host')
    report = integer(raw.get('report_host_ns'), 'report host')
    steps = raw.get('steps')
    require(isinstance(steps, list) and len(steps) == steps_n, 'step coverage')
    next_position, delivered, returned, previous_end = response['actual_input'], 0, 0, 0
    hist, census, step_results = [0] * 4, {}, []
    total_routes_hist = [0] * 5
    per_layer = [[0] * 5 for _ in range(48)]
    active_distribution = {}
    for index, step in enumerate(steps):
        object_value(step, 'step')
        for key, value in [('index', index), ('position', next_position),
                           ('k', 3), ('verify_rows', 4)]:
            equal(step, key, value)
        accepted = integer(step.get('accepted_drafts'), 'accepted drafts')
        require(accepted <= 3, 'accepted drafts exceeds k')
        equal(step, 'returned', accepted + 1)
        equal(step, 'extend_rows', accepted + 1)
        emitted = integer(step.get('delivered'), 'delivered', 1)
        require(emitted == min(accepted + 1, maximum - delivered), 'quota clip contract')
        require(index == steps_n - 1 or emitted == accepted + 1, 'nonfinal clipped step')
        timeline = named_marks(step.get('host_marks'), STEP_MARKS, 'step host')
        require(previous_end <= timeline['engine_begin'], 'overlapping host steps')
        previous_end = timeline['engine_end']
        parts = dict(zip(PHASES, host_partition(timeline, STEP_MARKS, 'step host')))
        layers = step.get('layers')
        require(isinstance(layers, list) and len(layers) == 48, '48-layer census coverage')
        for layer, row in enumerate(layers):
            sampled = index in SAMPLE_STEPS and layer in SAMPLE_LAYERS
            ch = census_row(row, layer, sampled)
            census[(index, layer)] = row
            for m, count in enumerate(ch):
                total_routes_hist[m] += count
                per_layer[layer][m] += count
            active = row['active_experts']
            active_distribution[active] = active_distribution.get(active, 0) + 1
        hist[accepted] += 1
        delivered += emitted
        returned += accepted + 1
        next_position += accepted + 1
        step_results.append({'index': index, 'position': step['position'],
                             'accepted_drafts': accepted, 'returned': accepted + 1,
                             'delivered': emitted, 'quota_clipped': accepted + 1 - emitted,
                             'host_marks_ns': timeline, 'phase_host_ns': parts,
                             'engine_host_ns': timeline['engine_end'] - timeline['engine_begin']})
    require(delivered == maximum, 'delivered sum differs from usage')
    expected_samples = [(step, layer) for step in SAMPLE_STEPS if step < steps_n
                        for layer in SAMPLE_LAYERS]
    equal(raw, 'sample_call_count', len(expected_samples))
    samples = raw.get('samples')
    require(isinstance(samples, list) and len(samples) == len(expected_samples),
            'fixed sample call coverage')
    sample_results, event_count, last_sample_end = [], 0, {}
    for sample, (step, layer) in zip(samples, expected_samples):
        equal(sample, 'step', step)
        equal(sample, 'layer', layer)
        result = validate_sample(sample, census[(step, layer)],
                                 step_results[step]['host_marks_ns'])
        require(last_sample_end.get(step, 0) <= result['host_ns']['moe_begin'],
                'sampled MoE host calls overlap')
        last_sample_end[step] = result['host_ns']['moe_end']
        sample_results.append(result)
        event_count += result['event_recorded']
    equal(raw, 'event_recorded', event_count)
    equal(raw, 'event_ready', event_count)
    require(event_count <= 496, 'event capacity exceeded')
    phase_total = {p: sum(step['phase_host_ns'][p] for step in step_results) for p in PHASES}
    steady = step_results[1:]
    return {'response_id': response['response_id'], 'prompt_tokens': response['actual_input'],
            'mtp_steps': steps_n, 'generated': delivered, 'engine_returned': returned,
            'quota_clipped': returned - delivered, 'accepted_drafts_histogram': hist,
            'accepted_draft_fraction': (returned - steps_n) / (3 * steps_n),
            'phase_host_sum_ns': phase_total,
            'engine_host_sum_ns': sum(phase_total.values()),
            'phase_ns_per_delivered_token': {p: v / delivered for p, v in phase_total.items()},
            'phase_ns_per_returned_token': {p: v / returned for p, v in phase_total.items()},
            'first_step': step_results[0], 'steps': step_results,
            'steady_step_count': len(steady),
            'steady_phase_host_sum_ns': {p: sum(s['phase_host_ns'][p] for s in steady) for p in PHASES},
            'census': {'calls': steps_n * 48, 'histogram': total_routes_hist,
                       'per_layer_histogram': per_layer,
                       'active_experts_distribution': active_distribution},
            'samples': sample_results, 'sample_call_count': len(samples),
            'absent_sample_steps': [s for s in SAMPLE_STEPS if s >= steps_n],
            'event_recorded': event_count, 'setup_host_ns': setup, 'report_host_ns': report,
            'sample_extrapolated_to_all_layers': False,
            'nested_experts_added_to_parent': False}


def audit_traces(log, responses, enabled):
    require('[q4t][mtp_cycle_timing]' not in log and
            '[q4t][mtp_init_timing]' not in log, 'old cycle/init collectors must be off')
    found = records(log)
    if not enabled:
        require(not found, 'disabled run emitted verify-MoE trace')
        return []
    ids = [r['response_id'] for r in responses]
    require(len(ids) == len(set(ids)), 'duplicate HTTP response ID')
    by_id = {}
    for row in found:
        rid = row.get('response_id')
        require(type(rid) is str and rid not in by_id, 'duplicate/missing trace ID')
        by_id[rid] = row
    require(set(by_id) == set(ids), 'trace/HTTP response ID coverage differs')
    return [validate_trace(by_id[r['response_id']], r) for r in responses]


def closed_wrapper(reader, path, enabled=False, current=True):
    name, parent = path.name, path.parent
    start = object_value(reader.json(parent / (name + '-start.json')), 'start')
    terminal = object_value(reader.json(parent / (name + '-wrapper-exit.json')), 'exit')
    sampler = object_value(reader.json(parent / (name + '-sampler-exit.json')), 'sampler')
    require(terminal.get('runner_exit_code') == 0 and terminal.get('wrapper_exit_code') == 0 and
            terminal.get('failure') is None, name + ': wrapper failed/incomplete')
    require(sampler.get('exit_code') == 0 and sampler.get('terminated_after_runner_exit') is False,
            name + ': sampler did not exit naturally')
    require(terminal.get('sampler') == sampler and terminal['end'] >= start['start'],
            name + ': sampler/time binding')
    for key in ('candidate_binary_sha256', 'candidate_identity_sha256', 'protocol_sha256', 'wrapper_sha256'):
        require(start.get(key) == terminal.get(key) and type(start.get(key)) is str,
                name + ': start/exit identity mismatch: ' + key)
    require(reader.bind(path / 'binary.sha256').decode().strip() == start['candidate_binary_sha256'],
            name + ': runner binary hash differs')
    require(Path(start['output']).resolve() == path and Path(terminal['output']).resolve() == path,
            name + ': output directory mismatch')
    env = {'Q4T_MTP_VERIFY_MOE_TIMING': '1'} if enabled else {}
    require(start.get('injected_environment') == env, name + ': injected environment')
    if current:
        require(reader.json(path / 'timing-audit.json').get('passed') is True,
                name + ': wrapper timing gate')
        if name != 'quality-off-01':
            require(reader.json(path / 'output-compatibility.json').get('passed') is True,
                    name + ': wrapper output gate')
    return {'start': start['start'], 'end': terminal['end'],
            'binary_sha256': start['candidate_binary_sha256'], 'sampler_exit': sampler['exit_code'],
            'protocol_sha256': start['protocol_sha256'], 'wrapper_sha256': start['wrapper_sha256'],
            'candidate_identity_sha256': start['candidate_identity_sha256']}


def extra_raw_bindings(reader, run):
    """Bind runner result/input/output files as well as load_run's raw DBs."""
    path = Path(run['directory'])
    results = reader.json(path / 'results.json')
    require(isinstance(results, list), 'results must be list')
    for index, row in enumerate(run['responses']):
        case = path / ('quality' if run['mode'] == 'quality' else f'context-{row["actual_input"]}')
        repeat = index if run['mode'] == 'quality' else row['repeat']
        require(reader.bind(case / f'output-{repeat}.txt').decode() == row['text'],
                'raw response/output text mismatch')
    cases = [path / 'quality'] if run['mode'] == 'quality' else [
        path / f'context-{length}' for length in LENGTHS]
    for case in cases:
        command = reader.json(case / 'command.json')
        require(isinstance(command, list), 'evalscope command must be list')
        for flag, value in [('--seed', '20260920'), ('--temperature', '0'),
                            ('--parallel', '1'), ('--warmup-num', '0'),
                            ('--max-tokens', '32' if run['mode'] == 'quality' else '256'),
                            ('--number', '11' if run['mode'] == 'quality' else '3')]:
            require(command.count(flag) == 1 and command[command.index(flag) + 1] == value,
                    'evalscope frozen argument differs: ' + flag)
        require('--stream' in command and '--no-stream' not in command and
                '--no-test-connection' in command, 'evalscope stream/probe contract')
        reader.bind(case / 'client.log')
    if run['mode'] == 'performance':
        require(len(results) == 5, 'performance result count')
        for length, saved in zip(LENGTHS, results):
            rows = run['per_length'][length]
            require(len({r['text_sha256'] for r in rows}) == 1, 'same-mode output changed')
            require(saved.get('length') == length and saved.get('decode_mode') == 'mtp' and
                    saved.get('deterministic') is True and
                    saved.get('prompt_sha256') == rows[0]['prompt_sha256'] and
                    saved.get('outputs') == [r['text_sha256'] for r in rows] and
                    saved.get('metrics') == [r['client_metrics'] for r in rows],
                    'performance results differ from raw observations')
            requests = [decode_json(line) for line in reader.bind(
                path / f'context-{length}' / 'requests.jsonl').decode().splitlines()]
            require(len(requests) == 3, 'fixture request count')
            for request, row in zip(requests, rows):
                prompt = request['prompt']
                require(hashlib.sha256(prompt.encode()).hexdigest() == row['prompt_sha256'],
                        'fixture/raw prompt hash differs')


def quality_gate(reader, protocol, quality):
    manifest = reader.json(Path(protocol['quality_fixtures']) / 'manifest.json')
    reference = reader.json(protocol['quality_reference'])
    results = reader.json(Path(quality['directory']) / 'results.json')
    require(all(isinstance(x, list) and len(x) == 11 for x in (manifest, reference, results)),
            'quality/reference/fixture coverage')
    fixtures = {r['prompt_sha256']: r for r in manifest}
    references = {r['id']: r for r in reference}
    require(len(fixtures) == len(references) == 11, 'duplicate quality identity')
    for row, saved in zip(quality['responses'], results):
        fixture = fixtures[row['prompt_sha256']]
        old = references[fixture['id']]
        require(all(saved.get(key) == row[key] for key in
                    ('text', 'actual_input', 'actual_output', 'prompt_sha256', 'response_id', 'finish')),
                'quality result differs from raw')
        require(saved.get('exact_match') is True and saved.get('length_match') is True and
                saved.get('id') == fixture['id'] and row['request_stream'] is True and
                row['actual_input'] == fixture['length'] and row['finish'] == ['stop'] and
                row['text'].strip() == fixture['expected'] and row['text'] == old['text'] and
                row['prompt_sha256'] == old['prompt_sha256'], 'quality reference contract')
    return {'passed': True, 'requests': 11}


def historical_snapshot_gate(reader, root, exit_record, snapshot_name):
    """Resolve historical sources through immutable snapshots, never current W."""
    identity_path = root.parent / 'candidate-identity.json'
    identity = object_value(reader.json(identity_path), 'historical identity')
    require(reader.sources[str(identity_path.resolve())]['sha256'] ==
            exit_record['candidate_identity_sha256'], 'historical identity bytes changed')
    equal(identity, 'binary_sha256', exit_record['binary_sha256'])
    snapshot = object_value(reader.json(root.parent / snapshot_name), 'historical snapshot')
    require(snapshot.get('candidate_binary_sha256', snapshot.get('binary_sha256')) ==
            exit_record['binary_sha256'], 'historical snapshot candidate differs')
    files = snapshot.get('files')
    require(isinstance(files, list) and files, 'missing historical source snapshot')
    by_original = {}
    for item in files:
        original, saved = Path(item['original_path']).resolve(), Path(item['snapshot_path']).resolve()
        require(str(original) not in by_original and original != saved,
                'duplicate/aliased historical snapshot path')
        by_original[str(original)] = item['sha256']
        check_bindings(reader, [{'path': str(saved), 'sha256': item['sha256']}], root.parent)
    bindings = identity['source_bindings']
    if isinstance(bindings, dict):
        bindings = [{'path': path, 'sha256': value.get('sha256') if isinstance(value, dict) else value}
                    for path, value in bindings.items()]
    require(isinstance(bindings, list) and bindings, 'historical runtime identity is empty')
    for item in bindings:
        require(by_original.get(str(Path(item['path']).resolve())) == item['sha256'],
                'historical runtime source absent/different in immutable snapshot')
    return {'passed': True, 'snapshot_file_count': len(files),
            'runtime_source_count': len(bindings),
            'current_original_source_paths_revalidated': False}


def strict_screen(comparison):
    cells = []
    for tier in comparison['tiers']:
        for metric in ('ttft', 'decode_seconds', 'latency'):
            baseline = [x[metric] for x in tier['left']['later']]
            candidate = [x[metric] for x in tier['right']['later']]
            require(len(baseline) == len(candidate) == 2, 'strict later repeat count')
            bound = max(baseline)
            cells.append({'length': tier['length'], 'metric': metric,
                          'baseline_later': baseline, 'candidate_later': candidate,
                          'baseline_later_max': bound,
                          'passed': all(value <= bound for value in candidate)})
    require(len(cells) == 15, 'strict screen cell coverage')
    return {'primary_binary_sha256': PRIMARY_SHA, 'cells': cells,
            'passed': all(cell['passed'] for cell in cells),
            'failed_cells': [cell for cell in cells if not cell['passed']],
            'diagnostic_build_observation_only': True, 'performance_acceptance': False}


def summarize_samples(requests):
    samples = [s for r in requests for s in r['samples']]
    expert_samples = [e for s in samples for e in s['expert_samples']]
    census = [sum(r['census']['histogram'][m] for r in requests) for m in range(5)]
    sampled_hist = [sum(s['census_histogram'][m] for s in samples) for m in range(5)]
    by_expert = []
    for rows in range(1, 5):
        for stream in range(4):
            for ordinal in SAMPLE_ORDINALS:
                if stream != ordinal % 4:
                    continue
                selected = [e for e in expert_samples if
                            (e['rows'], e['stream_index'], e['ordinal']) == (rows, stream, ordinal)]
                by_expert.append({'rows': rows, 'stream_index': stream, 'ordinal': ordinal,
                    'observations': len(selected),
                    'interval_ms_all': {name: [e['interval_ms'][name] for e in selected]
                                        for name in EXPERT_INTERVALS},
                    'mean_interval_ms': {name: statistics.mean(e['interval_ms'][name] for e in selected)
                                         for name in EXPERT_INTERVALS} if selected else None})
    timing_hist = [sum(e['rows'] == m for e in expert_samples) for m in range(5)]
    fractions = {}
    for name, hist in [('all_census', census), ('sampled_calls', sampled_hist),
                       ('timed_experts', timing_hist)]:
        experts, routes = sum(hist[1:]), sum(m * hist[m] for m in range(1, 5))
        fractions[name] = {
            'active_expert_fraction_by_rows': [hist[m] / experts if experts else None for m in range(1, 5)],
            'route_fraction_by_rows': [m * hist[m] / routes if routes else None for m in range(1, 5)]}
    categories = {}
    for name, selected in [('first_step', [s for s in samples if s['step'] == 0]),
                            ('later_fixed_steps', [s for s in samples if s['step'] != 0])]:
        categories[name] = {'calls': len(selected),
            'main_interval_mean_ms': {stage: statistics.mean(s['interval_ms'][stage] for s in selected)
                                      for stage in GPU_INTERVALS} if selected else None,
            'main_total_all_ms': [s['stream_total_ms'] for s in selected]}
    return {'census_calls': sum(r['census']['calls'] for r in requests),
            'census_histogram': census, 'sample_calls': len(samples),
            'sampled_call_histogram': sampled_hist,
            'timed_expert_histogram': timing_hist, 'distribution_comparison': fractions,
            'main_stream_interval_sum_ms': {name: math.fsum(s['interval_ms'][name] for s in samples)
                                            for name in GPU_INTERVALS},
            'sample_main_stream_total_sum_ms': math.fsum(s['stream_total_ms'] for s in samples),
            'first_step_sample_calls': sum(s['step'] == 0 for s in samples),
            'later_sample_calls': sum(s['step'] != 0 for s in samples),
            'first_and_later_samples': categories,
            'absent_ordinals': [{'step': s['step'], 'layer': s['layer'], 'ordinal': o}
                                for s in samples for o in s['absent_ordinals']],
            'expert_timing_by_rows_stream_ordinal': by_expert,
            'expert_chain_sums_added_to_parallel_window': False,
            'extrapolated_total_verify_moe_cost': None}


def analyze(quality, control, diagnostic, primary, development, reader=None):
    reader = reader or Reader()
    for name in IMPORT_SOURCES:
        reader.bind(Path(__file__).resolve().parent / name)
    paths = dict(zip(('quality', 'control', 'diagnostic', 'primary', 'development'),
                     (Path(p).resolve() for p in (quality, control, diagnostic, primary, development))))
    require(paths['quality'].parent == paths['control'].parent == paths['diagnostic'].parent,
            'current groups must share one frozen stage')
    exits = {name: closed_wrapper(reader, path, name == 'diagnostic',
                                  name in ('quality', 'control', 'diagnostic'))
             for name, path in paths.items()}
    require(len({exits[n]['binary_sha256'] for n in ('quality', 'control', 'diagnostic')}) == 1,
            'current paired groups used different binaries')
    require(exits['primary']['binary_sha256'] == PRIMARY_SHA and
            exits['development']['binary_sha256'] == DEVELOPMENT_SHA,
            'primary/development identity changed')
    require(exits['quality']['end'] <= exits['control']['start'] and
            exits['control']['end'] <= exits['diagnostic']['start'], 'frozen group order')
    stage = paths['quality'].parent
    review = object_value(reader.json(stage / 'instrumentation-review.json'), 'instrumentation gate')
    for name in ('passed', 'quality_http_passed', 'host_contract_passed',
                 'runtime_identity_review_passed', 'device_code_identity_review_passed'):
        equal(review, name, True)
    require(review.get('candidate_binary_sha256') == exits['quality']['binary_sha256'],
            'instrumentation candidate differs')
    identity = identity_gate(reader, stage, {n: exits[n] for n in
                             ('quality', 'control', 'diagnostic')}, review)
    historical_identity = {
        'primary': historical_snapshot_gate(reader, paths['primary'], exits['primary'],
                                             'diagnostic-source-snapshot.json'),
        'development': historical_snapshot_gate(reader, paths['development'], exits['development'],
                                                 'source-snapshot.json')}
    protocol = reader.json(stage / 'http-protocol.json')
    runs, traces = {}, {}
    for name, path in paths.items():
        run = load_run(reader, path, 'quality' if name == 'quality' else 'performance', False)
        require(hashlib.sha256(reader.bind(Path(run['binary_path']))).hexdigest() ==
                exits[name]['binary_sha256'], name + ': actual binary differs')
        extra_raw_bindings(reader, run)
        traces[name] = audit_traces(reader.bind(path / 'server.log').decode(),
                                    run['responses'], name == 'diagnostic')
        runs[name] = run
    quality_result = quality_gate(reader, protocol, runs['quality'])
    comparisons = [add_client_ranges(compare_runs(runs[a], runs[b], label)) for a, b, label in (
        ('control', 'diagnostic', 'same-binary disabled -> verify-MoE diagnostic'),
        ('primary', 'control', 'frozen primary21d -> new disabled; primary is not reset'),
        ('development', 'control', 'developmentC2 -> new disabled; auxiliary only'))]
    by_id = {row['response_id']: row for row in traces['diagnostic']}
    tiers = []
    for length in LENGTHS:
        requests = [by_id[row['response_id']] for row in runs['diagnostic']['per_length'][length]]
        require(all(r['sample_call_count'] == 16 and not r['absent_sample_steps'] for r in requests),
                'formal matrix must cover all16 fixed sample calls')
        phase = {p: sum(r['phase_host_sum_ns'][p] for r in requests) for p in PHASES}
        generated = sum(r['generated'] for r in requests)
        tiers.append({'length': length, 'requests': requests,
                      'accepted_drafts_histogram': [sum(r['accepted_drafts_histogram'][i] for r in requests)
                                                    for i in range(4)],
                      'phase_host_sum_ns': phase,
                      'phase_host_mean_per_request_ns': {p: n / 3 for p, n in phase.items()},
                      'phase_host_ns_per_delivered_token': {p: n / generated for p, n in phase.items()},
                      'coverage_and_sample_statistics': summarize_samples(requests)})
    return {'schema_version': 1, 'passed': all(c['output_and_path_equal'] for c in comparisons),
            'performance_acceptance': False, 'default_enablement': False,
            'previous_no_go_retained': True, 'quality_gate': quality_result,
            'closed_groups': exits, 'identity_gate': identity,
            'historical_source_snapshots': historical_identity,
            'coverage': {'new_quality': 11, 'new_control': 15, 'new_diagnostic': 15,
                         'new_http_total': 41, 'reused_primary': 15, 'reused_development': 15,
                         'valid_diagnostic_records': len(traces['diagnostic'])},
            'comparisons': comparisons, 'primary_strict_screen': strict_screen(comparisons[1]),
            'timing_tiers': tiers,
            'memory': {n: sampled_memory(reader, paths[n]) for n in ('quality', 'control', 'diagnostic')},
            'sources': reader.sources,
            'limits': [
                'Census covers all actual verify steps and48 layers; event timings cover only the frozen16 calls. No all-layer timing extrapolation.',
                'Active experts are counted before clamp; census is not token-to-expert routing or weight contribution.',
                'GPU intervals are same-stream elapsed time, including submission gaps/contention; not pure GPU active time.',
                'parallel_window includes offsets/mapping, host preparation and overlapping expert work. Sampled expert chains are nested and never added across streams or to the main window.',
                'Host integer-nanosecond phases contain existing preparation and waits. Host counts waiting includes prior GPU work and is not removable synchronization overhead.',
                'Fixed binary32/binary64 ULP closure checks arithmetic consistency, not absolute CUDA timing accuracy or model numerical error.',
                'GPU-mark at_ns is the host record-call timestamp, not a GPU execution timestamp. Host, stream and client clocks are not subtracted or added.',
                'report_host_ns covers only the initial JSON construction; it excludes Query/Elapsed, Destroy, out.str and fprintf. Postprocessing occurs after SSE done and may affect the next request; this field is not total observer overhead.',
                'Engine returned and delivered tokens differ under final quota clipping. Whole-step engine time is not client255-token decode time.',
                'Only first1K is first after loading; first step of each request is separately recorded. Absent histogram bins/ordinals are not invented.',
                'Fixed off-then-on order and three repeats cannot prove causal observer cost, zero overhead, or stable tail latency.',
                'Original21d remains the strict primary; C2 is auxiliary. Diagnostic completeness and strict observations do not approve a candidate or enable MTP.',
                'Identity and finite numerical reuse are independently reviewed. Exact HTTP text is not a whole-model or cross-mode numerical oracle.',
                'Sampling reports process RSS/HWM/Swap and system MemAvailable separately; no physical RAM peak or difference guarantee.'
            ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('quality', 'control', 'diagnostic', 'primary', 'development', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    require(not args.output.exists(), 'refuse to overwrite analysis output')
    reader = Reader()
    try:
        result = analyze(args.quality, args.control, args.diagnostic,
                         args.primary, args.development, reader)
    except Exception as error:
        result = {'schema_version': 1, 'passed': False, 'analysis_incomplete': True,
                  'error': type(error).__name__ + ': ' + str(error),
                  'sources': reader.sources, 'performance_acceptance': False,
                  'previous_no_go_retained': True}
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'passed': result['passed'], 'output': str(args.output)}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
