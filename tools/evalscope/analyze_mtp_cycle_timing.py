"""Audit recorded S1 MTP cycles and initialization; never launch inference.

Only trusted local evalscope SQLite/pickle evidence is accepted. Cycle timing
uses one steady_clock host domain. TLS detail spans are nested observations,
not additional parent costs. Existing initialization CUDA contracts stay in
analyze_mtp_init_timing and are not combined with cycle host durations.
"""
import argparse
import hashlib
import math
from pathlib import Path
import statistics
import json

from analyze_mtp_init_timing import (
    Reader, LENGTHS, compare_runs,
    decode_json, load_run, number, require)

PREFIX = '[q4t][mtp_cycle_timing] '
REQUEST_MARKS = ('timing_setup_begin', 'timing_setup_end', 'main_prefill_finished',
                 'init_finished', 'first_content_write_begin',
                 'first_content_write_end', 'request_end')
STEP_MARKS = ('submit', 'scheduler_pick', 'model_lock_acquired', 'engine_begin',
              'draft_end', 'verify_pack_end', 'verify_model_end',
              'verify_readback_end', 'accept_end', 'extend_pack_end',
              'extend_gather_end', 'extend_model_end', 'extend_readback_end',
              'engine_end', 'scheduler_finish', 'request_wake', 'emit_begin',
              'emit_end', 'advance_end')
PARTITION_NAMES = ('queue', 'scheduler_prepare_and_model_lock', 'engine_dispatch',
                   'draft', 'verify_pack', 'verify_model', 'verify_readback',
                   'accept_restore', 'extend_pack', 'extend_gather',
                   'extend_model', 'extend_readback', 'engine_result',
                   'scheduler_publish', 'request_wake', 'request_before_emit',
                   'emit', 'advance')
PHASES = ('draft', 'verify', 'accept', 'extend')
DETAILS = ('positions_readback', 'draft_moe_counts', 'verify_moe_counts',
           'linear_alloc', 'linear_free', 'gather_alloc', 'gather_free',
           'mtp_forward', 'mtp_attention', 'mtp_moe', 'mtp_head')
PHASE_BOUNDS = {'draft': ('engine_begin', 'draft_end'),
                'verify': ('draft_end', 'verify_readback_end'),
                'accept': ('verify_readback_end', 'accept_end'),
                'extend': ('accept_end', 'engine_end')}
# Frozen successful S1/k3/48-layer model path, established by source review.
# Counts cover all leaf hooks; zero entries are required, not silently absent.
EXPECTED_DETAIL_CALLS = {p: {n: 0 for n in DETAILS} for p in PHASES}
for _phase, _count in [('draft', 2), ('extend', 1)]:
    for _name in ('positions_readback', 'draft_moe_counts', 'mtp_forward',
                  'mtp_attention', 'mtp_moe', 'mtp_head'):
        EXPECTED_DETAIL_CALLS[_phase][_name] = _count
EXPECTED_DETAIL_CALLS['extend'].update(gather_alloc=1, gather_free=1)
EXPECTED_DETAIL_CALLS['verify'].update(verify_moe_counts=48, linear_alloc=216, linear_free=216)


def integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum, label + ': invalid integer')
    return value


def equal_field(record, name, expected):
    value = record.get(name)
    require(value == expected and type(value) is type(expected),
            f'{name}: expected {expected!r}, got {value!r}')


def marks(values, names, label):
    require(isinstance(values, list) and len(values) == len(names), label + ': marker count')
    result, previous = {}, 0.0
    for value, name in zip(values, names):
        require(isinstance(value, dict) and value.get('name') == name, label + ': marker order')
        at = number(value.get('at_ms'), label + '/' + name)
        require(at >= previous, label + ': nonmonotonic clock')
        result[name] = at
        previous = at
    return result


def host_roundoff_budget(endpoint, terms):
    """Conservative fixed arithmetic budget, never a measured-cost tolerance.

    Sixteen binary64 ULPs of the largest absolute timestamp per term cover
    serialized timestamp subtraction and summation. No threshold is fitted to
    observed duration or speed. This is not clock precision or GPU accuracy.
    """
    return 16 * math.ulp(max(1.0, endpoint)) * (terms + 1)


def records(log):
    result = []
    for line in log.splitlines():
        if '[q4t][mtp_cycle_timing]' not in line:
            continue
        require(line.startswith(PREFIX), 'malformed cycle trace prefix')
        value = decode_json(line[len(PREFIX):])
        require(isinstance(value, dict), 'cycle trace must be object')
        result.append(value)
    return result


def validate_cycle(raw, response):
    rid = response['response_id']
    for key, expected in [('schema_version', 1), ('response_id', rid),
                          ('prompt_tokens', response['actual_input']), ('k', 3),
                          ('max_seq', 1), ('text_only', True), ('complete', True),
                          ('valid', True), ('error', ''), ('success', True),
                          ('plain_tail', False), ('fallback', 'none'),
                          ('diagnostic_only', True), ('host_origin', 'handle_chat_entry'),
                          ('unit', 'ms'), ('clock', 'steady_clock'),
                          ('contains_cuda_events', False), ('host_details_additive', False),
                          ('generated', response['actual_output'])]:
        equal_field(raw, key, expected)
    require(response['finish'] == ['length'], rid + ': diagnostic requires length finish')
    equal_field(raw, 'finish_reason', 'length')
    maximum = integer(raw.get('max_tokens'), rid + '/max_tokens', 1)
    equal_field(raw, 'max_tokens', response['actual_output'])
    equal_field(raw, 'step_capacity', min(maximum, 1024))
    total_steps = integer(raw.get('mtp_steps'), rid + '/mtp_steps', 1)
    equal_field(raw, 'mtp_steps', int(response['actual_path']['mtp_steps']))
    require(total_steps <= raw['step_capacity'], rid + ': step capacity exceeded')
    setup = number(raw.get('setup_host_ms'), rid + '/setup')
    report_cost = number(raw.get('report_host_ms'), rid + '/report')
    request = marks(raw.get('request_marks'), REQUEST_MARKS, rid + '/request')
    values = raw.get('steps')
    require(isinstance(values, list) and len(values) == total_steps, rid + ': step coverage')
    step_results, histogram, generated, returned = [], [0] * 4, 0, 0
    next_position, previous_end = response['actual_input'], request['init_finished']
    for index, step in enumerate(values):
        require(isinstance(step, dict), rid + ': invalid step object')
        for key, expected in [('index', index), ('position', next_position), ('k', 3), ('verify_rows', 4)]:
            equal_field(step, key, expected)
        accepted = integer(step.get('accepted_drafts'), rid + '/accepted_drafts')
        require(accepted <= 3, rid + ': accepted drafts exceeds k')
        equal_field(step, 'returned', 1 + accepted)
        equal_field(step, 'extend_rows', 1 + accepted)
        delivered = integer(step.get('generated'), rid + '/step generated', 1)
        writes = integer(step.get('token_piece_writes'), rid + '/writes')
        nonempty = integer(step.get('nonempty_writes'), rid + '/nonempty')
        require(delivered == min(1 + accepted, maximum - generated), rid + ': quota clipping mismatch')
        require(writes == delivered and nonempty <= writes, rid + ': SSE delivered count mismatch')
        require(index == total_steps - 1 or delivered == 1 + accepted,
                rid + ': nonfinal clipped step')
        timeline = marks(step.get('marks'), STEP_MARKS, f'{rid}/step{index}')
        require(previous_end <= timeline['submit'] <= timeline['advance_end'] <= request['request_end'],
                rid + ': step/request chronology')
        gap = timeline['submit'] - previous_end
        previous_end = timeline['advance_end']
        partition = {name: timeline[STEP_MARKS[i + 1]] - timeline[STEP_MARKS[i]]
                     for i, name in enumerate(PARTITION_NAMES)}
        duration = timeline['advance_end'] - timeline['submit']
        residual = duration - math.fsum(partition.values())
        budget = host_roundoff_budget(request['request_end'], len(partition))
        require(abs(residual) <= budget, rid + ': host partition does not close')
        phase = {name: timeline[end] - timeline[begin] for name, (begin, end) in PHASE_BOUNDS.items()}
        detail_rows = step.get('details')
        require(isinstance(detail_rows, list) and len(detail_rows) == 44, rid + ': detail coverage')
        detail = {name: {} for name in PHASES}
        for item, (parent, name) in zip(detail_rows, ((p, n) for p in PHASES for n in DETAILS)):
            equal_field(item, 'phase', parent)
            equal_field(item, 'name', name)
            calls = integer(item.get('calls'), rid + '/detail calls')
            require(calls == EXPECTED_DETAIL_CALLS[parent][name],
                    rid + ': leaf hook call coverage ' + parent + '/' + name)
            elapsed = number(item.get('host_ms'), rid + '/detail duration')
            require(calls != 0 or elapsed == 0, rid + ': zero-call detail has duration')
            allowance = host_roundoff_budget(request['request_end'], calls + 2)
            require(elapsed <= phase[parent] + allowance, rid + ': detail exceeds parent')
            detail[parent][name] = {'calls': calls, 'host_ms': elapsed}
        if index == 0:
            require(nonempty > 0 and timeline['emit_begin'] <= request['first_content_write_begin'] <=
                    request['first_content_write_end'] <= timeline['emit_end'], rid + ': first-content coverage')
        histogram[accepted] += 1
        generated += delivered
        returned += 1 + accepted
        next_position += 1 + accepted
        step_results.append({'index': index, 'position': step['position'], 'accepted_drafts': accepted,
                             'engine_returned': 1 + accepted, 'generated': delivered,
                             'quota_clipped': 1 + accepted - delivered,
                             'token_piece_writes': writes, 'nonempty_writes': nonempty,
                             'verify_rows': 4, 'extend_rows': 1 + accepted,
                             'partition_ms': partition, 'phase_host_ms': phase,
                             'details': detail, 'submit_to_advance_ms': duration,
                             'engine_ms': timeline['engine_end'] - timeline['engine_begin'],
                             'gap_before_submit_ms': gap, 'marks': timeline,
                             'closure_residual_ms': residual, 'closure_budget_ms': budget})
    require(generated == maximum == response['actual_output'], rid + ': generated sum/usage mismatch')
    window = request['request_end'] - request['init_finished']
    tail_gap = request['request_end'] - previous_end
    cycle_sum = math.fsum(x['submit_to_advance_ms'] for x in step_results)
    gap_sum = math.fsum([x['gap_before_submit_ms'] for x in step_results] + [tail_gap])
    closure = window - math.fsum([cycle_sum, gap_sum])
    budget = host_roundoff_budget(request['request_end'], 2 * total_steps + 2)
    require(abs(closure) <= budget, rid + ': request cycle window does not close')
    steady = step_results[1:]
    unclipped_steady = [x for x in steady if x['quota_clipped'] == 0]
    result = {'response_id': rid, 'prompt_tokens': response['actual_input'], 'generated': generated,
              'engine_returned': returned, 'quota_clipped': returned - generated,
              'mtp_steps': total_steps, 'accepted_drafts_histogram': histogram,
              'accepted_drafts_total': returned - total_steps,
              'accepted_draft_fraction': (returned - total_steps) / (3 * total_steps),
              'generated_tokens_per_step': generated / total_steps,
              'verify_rows_per_generated_token': (4 * total_steps) / generated,
              'draft_forward_calls': 2 * total_steps, 'extend_rows': returned,
              'request_marks_ms': request, 'steps': step_results,
              'first_step': step_results[0], 'last_step': step_results[-1],
              'steady_step_count': len(steady), 'unclipped_steady_step_count': len(unclipped_steady),
              'sum_partition_ms': {n: math.fsum(x['partition_ms'][n] for x in step_results) for n in PARTITION_NAMES},
              'sum_phase_host_ms': {p: math.fsum(x['phase_host_ms'][p] for x in step_results) for p in PHASES},
              'sum_details': {p: {n: {'calls': sum(x['details'][p][n]['calls'] for x in step_results),
                                      'host_ms': math.fsum(x['details'][p][n]['host_ms'] for x in step_results)}
                                   for n in DETAILS} for p in PHASES},
              'cycle_window_ms': window, 'sum_submit_to_advance_ms': cycle_sum,
              'unassigned_cycle_window_ms': gap_sum, 'cycle_window_closure_ms': closure,
              'cycle_window_closure_budget_ms': budget, 'setup_host_ms': setup, 'report_host_ms': report_cost,
              'nested_details_added_to_parent': False}
    result['cost_per_generated_token_ms'] = {p: v / generated for p, v in result['sum_phase_host_ms'].items()}
    result['cost_per_engine_returned_token_ms'] = {p: v / returned for p, v in result['sum_phase_host_ms'].items()}
    result['steady_cost_per_generated_token_ms'] = (
        {p: math.fsum(x['phase_host_ms'][p] for x in steady) / sum(x['generated'] for x in steady) for p in PHASES}
        if steady else None)
    result['acceptance_conditioned'] = []
    for accepted in range(4):
        selected = [x for x in step_results if x['accepted_drafts'] == accepted]
        result['acceptance_conditioned'].append({'accepted_drafts': accepted, 'steps': len(selected),
            'phase_mean_ms': {p: statistics.mean(x['phase_host_ms'][p] for x in selected) for p in PHASES} if selected else None,
            'engine_mean_ms': statistics.mean(x['engine_ms'] for x in selected) if selected else None})
    return result


def audit_cycles(log, responses, enabled):
    found = records(log)
    if not enabled:
        require(not found, 'cycle-disabled run emitted records')
        return []
    ids = [r['response_id'] for r in responses]
    require(len(ids) == len(set(ids)), 'duplicate HTTP IDs')
    by_id = {}
    for record in found:
        rid = record.get('response_id')
        require(type(rid) is str and rid not in by_id, 'duplicate/malformed cycle response ID')
        by_id[rid] = record
    require(set(by_id) == set(ids), 'cycle trace does not cover actual HTTP IDs')
    return [validate_cycle(by_id[r['response_id']], r) for r in responses]


def closed_wrapper(reader, root):
    parent, name = root.parent, root.name
    start = reader.json(parent / (name + '-start.json'))
    terminal = reader.json(parent / (name + '-wrapper-exit.json'))
    sampler = reader.json(parent / (name + '-sampler-exit.json'))
    require(all(isinstance(x, dict) for x in (start, terminal, sampler)), name + ': terminal records must be objects')
    require(terminal.get('runner_exit_code') == 0 and terminal.get('wrapper_exit_code') == 0 and
            terminal.get('failure') is None, name + ': wrapper incomplete/failed')
    require(sampler.get('exit_code') == 0 and sampler.get('terminated_after_runner_exit') is False,
            name + ': sampler incomplete/failed')
    require(terminal.get('sampler') == sampler and terminal['end'] >= start['start'], name + ': terminal binding')
    require(reader.bind(root / 'binary.sha256').decode().strip() == terminal['candidate_binary_sha256'],
            name + ': candidate identity mismatch')
    for key in ('candidate_binary_sha256', 'candidate_identity_sha256', 'protocol_sha256', 'wrapper_sha256'):
        require(start.get(key) == terminal.get(key), name + ': start/end ' + key)
    expected = ({'Q4T_MTP_CYCLE_TIMING': '1', 'Q4T_MTP_INIT_TIMING': '1'}
                if name == 'diagnostic-on-01' else {})
    require(start.get('injected_environment') == expected, name + ': injected environment')
    require(reader.json(root / 'timing-audit.json')['passed'] is True, name + ': wrapper trace gate')
    if name != 'quality-off-01':
        require(reader.json(root / 'output-compatibility.json')['passed'] is True, name + ': compatibility gate')
    return {'start': start['start'], 'end': terminal['end'],
            'binary_sha256': terminal['candidate_binary_sha256'], 'sampler_exit': sampler['exit_code'],
            'protocol_sha256': start['protocol_sha256'], 'wrapper_sha256': start['wrapper_sha256'],
            'candidate_identity_sha256': start['candidate_identity_sha256']}


def check_bindings(reader, bindings, base):
    if isinstance(bindings, dict):
        bindings = [{'path': key, 'sha256': value['sha256'] if isinstance(value, dict) else value}
                    for key, value in bindings.items()]
    require(isinstance(bindings, list), 'source bindings must be list or map')
    for binding in bindings:
        path = Path(binding['path'])
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        require('/models/dev/llm/' not in str(path), 'model payload access forbidden')
        require(hashlib.sha256(reader.bind(path)).hexdigest() == binding['sha256'], 'source binding changed: ' + str(path))


def identity_gate(reader, stage, exits, review):
    protocol_path = stage / 'http-protocol.json'
    identity_path = stage / 'candidate-identity.json'
    protocol = reader.json(protocol_path)
    identity = reader.json(identity_path)
    require(isinstance(protocol, dict) and isinstance(identity, dict), 'identity/protocol must be objects')
    root = Path(protocol['worktree'])
    for record in exits.values():
        for path, field in [(protocol_path, 'protocol_sha256'), (identity_path, 'candidate_identity_sha256'),
                            (stage / 'run-http.py', 'wrapper_sha256')]:
            require(hashlib.sha256(reader.bind(path)).hexdigest() == record[field], 'frozen ' + field + ' mismatch')
        require(record['binary_sha256'] == identity['binary_sha256'], 'group used another binary')
    check_bindings(reader, protocol['source_bindings'], root)
    check_bindings(reader, identity['source_bindings'], root)
    check_bindings(reader, review['source_bindings'], root)
    binary = Path(identity['binary'])
    library = identity.get('library', str(binary.parent / 'libq4t_model.a'))
    library_hash = identity.get('library_sha256', identity.get('model_library_sha256'))
    if isinstance(library, dict):
        library_hash, library = library.get('sha256', library_hash), library['path']
    check_bindings(reader, [{'path': str(binary), 'sha256': identity['binary_sha256']},
                           {'path': str(library), 'sha256': library_hash},
                           {'path': str(binary.parent / 'CMakeCache.txt'), 'sha256': identity['cmake_cache_sha256']}], root)
    return {'candidate_binary_sha256': identity['binary_sha256'],
            'candidate_library_sha256': library_hash, 'runtime_source_count': len(identity['source_bindings'])}


def add_client_ranges(comparison):
    for tier in comparison['tiers']:
        for side in ('left', 'right'):
            row = tier[side]
            row['arithmetic_means'] = {key: statistics.mean(x[key] for x in row['all'])
                for key in ('ttft', 'latency', 'decode_seconds', 'decode_tps', 'overall_tps')}
            row['later_ranges'] = {key: {'min': min(x[key] for x in row['later']),
                                        'max': max(x[key] for x in row['later'])}
                for key in row['arithmetic_means']}
    return comparison


def sampled_memory(reader, root):
    rows = [decode_json(line) for line in reader.bind(
        root.parent / (root.name + '-memory.jsonl')).decode().splitlines()]
    require(len(rows) > 1, 'memory sampling has insufficient coverage')
    times = [number(r['unix_time'], 'memory timestamp') for r in rows]
    require(all(a < b for a, b in zip(times, times[1:])), 'memory sample timestamps unordered')
    fields = {}
    for group, names in [('process', ('VmRSS_kib', 'VmHWM_kib', 'VmSwap_kib')),
                         ('system', ('MemAvailable_kib',))]:
        for name in names:
            values = [integer(r[group][name], 'memory field') for r in rows if name in r[group]]
            require(values, 'memory field missing: ' + name)
            fields[name] = {'covered_samples': len(values), 'min': min(values), 'max': max(values)}
    return {'sample_count': len(rows), 'server_pids': sorted({r['pid'] for r in rows}),
            'first_unix': times[0], 'last_unix': times[-1], 'span_seconds': times[-1] - times[0],
            'maximum_sample_gap_seconds': max(b - a for a, b in zip(times, times[1:])),
            'fields': fields, 'physical_ram_difference_or_peak_guarantee': False}


def analyze(quality, control, diagnostic, historical):
    reader = Reader()
    qpath, cpath, dpath, hpath = (Path(x).resolve() for x in (quality, control, diagnostic, historical))
    exits = {name: closed_wrapper(reader, path) for name, path in
             [('quality', qpath), ('control', cpath), ('diagnostic', dpath)]}
    require(len({x['binary_sha256'] for x in exits.values()}) == 1, 'new groups differ in candidate identity')
    require(exits['quality']['end'] <= exits['control']['start'] and
            exits['control']['end'] <= exits['diagnostic']['start'], 'frozen group order')
    review = reader.json(qpath.parent / 'instrumentation-review.json')
    require(isinstance(review, dict), 'instrumentation review must be object')
    for key in ('passed', 'quality_http_passed', 'host_contract_passed',
                'runtime_identity_review_passed', 'device_code_identity_review_passed'):
        require(review.get(key) is True, 'instrumentation gate: ' + key)
    require(review.get('candidate_binary_sha256') == exits['quality']['binary_sha256'], 'review candidate identity')
    identity = identity_gate(reader, qpath.parent, exits, review)
    runs = {name: load_run(reader, path, kind, enabled) for name, path, kind, enabled in
            [('quality', qpath, 'quality', False), ('control', cpath, 'performance', False),
             ('diagnostic', dpath, 'performance', True), ('historical', hpath, 'performance', False)]}
    cycles = {}
    for name, run in runs.items():
        root = Path(run['directory'])
        log = reader.bind(root / 'server.log').decode()
        cycles[name] = audit_cycles(log, run['responses'], name == 'diagnostic')
        cases = [root / 'quality'] if name == 'quality' else [root / f'context-{n}' for n in LENGTHS]
        for case in cases:
            saved = reader.json(case / 'responses.json')
            for index, row in enumerate(saved):
                require(reader.bind(case / f'output-{index}.txt').decode() == row['text'], 'saved output mismatch')
    q, c, d, h = (runs[n] for n in ('quality', 'control', 'diagnostic', 'historical'))
    comparisons = [add_client_ranges(compare_runs(c, d, 'same-build control -> complete-cycle diagnostic')),
                   add_client_ranges(compare_runs(h, c, 'historical0d3495c8 -> new control')),
                   add_client_ranges(compare_runs(h, d, 'historical0d3495c8 -> new diagnostic'))]
    by_id = {r['response_id']: r for r in cycles['diagnostic']}
    init_by_id = {r['response_id']: r for r in d['traces']}
    tiers = []
    for length in LENGTHS:
        rows = [by_id[r['response_id']] for r in d['per_length'][length]]
        generated = sum(r['generated'] for r in rows)
        returned = sum(r['engine_returned'] for r in rows)
        steps = sum(r['mtp_steps'] for r in rows)
        totals = {p: math.fsum(r['sum_phase_host_ms'][p] for r in rows) for p in PHASES}
        hist = [sum(r['accepted_drafts_histogram'][i] for r in rows) for i in range(4)]
        tiers.append({'length': length, 'requests': rows,
            'initialization_requests': [init_by_id[r['response_id']] for r in rows],
            'total_steps': steps, 'generated_tokens': generated, 'engine_returned_tokens': returned,
            'quota_clipped_tokens': returned - generated, 'accepted_drafts_histogram': hist,
            'accepted_draft_fraction': (returned - steps) / (3 * steps),
            'generated_tokens_per_step': generated / steps,
            'phase_sum_ms': totals, 'phase_mean_per_request_ms': {p: v / 3 for p, v in totals.items()},
            'phase_cost_per_generated_token_ms': {p: v / generated for p, v in totals.items()},
            'phase_cost_per_engine_returned_token_ms': {p: v / returned for p, v in totals.items()},
            'all_acceptance_counts_observed': all(hist),
            'nested_details_added_to_parent': False})
    return {'schema_version': 1, 'passed': all(x['output_and_path_equal'] for x in comparisons),
            'performance_acceptance': False, 'default_enablement': False,
            'previous_no_go_retained': True, 'closed_groups': exits, 'identity_gate': identity,
            'coverage': {'quality_off': 11, 'control_off': 15, 'diagnostic_on': 15,
                         'new_http_total': 41, 'historical_auxiliary': 15,
                         'valid_cycle_records': len(cycles['diagnostic']), 'valid_init_records': len(d['traces'])},
            'comparisons': comparisons, 'cycle_tiers': tiers,
            'memory': {name: sampled_memory(reader, root) for name, root in
                       [('quality', qpath), ('control', cpath), ('diagnostic', dpath)]},
            'sources': reader.sources,
            'limits': [
                'Cycle host phase windows contain submission and existing waits; they are not GPU active time.',
                'TLS detail intervals nest inside phases and sometimes each other; never sum them into parent or label every wait as API overhead.',
                'Initialization CUDA events and client timings have different clock domains; no cross-clock subtraction or addition.',
                'Generated includes any EOS in the generic runtime; this finite matrix requires length256, no EOS/fallback. Successful SSE pieces and nonempty pieces are not token usage.',
                'Engine returned tokens can exceed delivered output on final quota clipping; cost denominators remain explicit.',
                'First-step engine work precedes first content and can produce a burst. Client255-token decode is not the sum of complete post-first engine steps.',
                'Only the first1K request of each service is its first request; first step of every request is separately labeled.',
                'No natural occurrence of an acceptance count is invented; missing bins remain zero and do not imply tested engine behavior.',
                'Fixed off-then-on order and three repeats per tier do not prove causal observer cost, zero overhead, or performance acceptance.',
                'Identical output is scoped identity evidence, not whole-model/cross-mode numerical proof. Previous NO_GO remains.'
            ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('quality', 'control', 'diagnostic', 'historical', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    require(not args.output.exists(), 'refuse to overwrite analysis output')
    try:
        result = analyze(args.quality, args.control, args.diagnostic, args.historical)
    except Exception as error:
        result = {'schema_version': 1, 'passed': False, 'analysis_incomplete': True,
                  'error': type(error).__name__ + ': ' + str(error), 'performance_acceptance': False}
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'passed': result['passed'], 'output': str(args.output)}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
