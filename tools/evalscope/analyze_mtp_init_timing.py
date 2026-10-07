"""Audit already-recorded HTTP initialization timing; never launch a server.

Only open trusted evalscope databases created locally by this repository:
response_messages uses the runner's existing pickle encoding. No model payload
or device is accessed. Host, CUDA stream intervals and client timing remain
separate measurements, including their different coverage and overlap.
"""
import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
import pickle
import sqlite3
import statistics
import struct

from acceptance_mode import (performance_metrics, request_mode_evidence,
                             startup_evidence)
from response_identity import response_identity

PREFIX = '[q4t][mtp_init_timing] '
LENGTHS = (1024, 4096, 8192, 45056, 204800)
STAGES = ('input_copy', 'setup_h2d', 'projections', 'attn_hc', 'attention',
          'mlp_hc', 'moe', 'mixer', 'head')
HOST_BEFORE = ('timing_setup_begin', 'timing_setup_end',
               'trunk_prepare_begin', 'trunk_alloc_begin', 'trunk_alloc_end',
               'trunk_prepare_end', 'main_lock_wait_begin', 'main_lock_acquired',
               'main_reset_begin', 'main_reset_end', 'main_prefill_begin')
HOST_AFTER = ('main_prefill_end', 'prefill_argmax_begin', 'prefill_argmax_end',
              'rolling_trunk_alloc_begin', 'rolling_trunk_alloc_end',
              'init_lock_wait_begin', 'init_lock_acquired', 'draft_reset_begin',
              'draft_reset_end', 'shift_begin', 'shift_end', 'extend_begin',
              'extend_alloc_begin', 'extend_alloc_end', 'extend_finalize_begin',
              'extend_cleanup_begin', 'extend_cleanup_end', 'extend_end',
              'checkpoint_reserve_begin', 'checkpoint_reserve_end',
              'scratch_reserve_begin', 'scratch_reserve_end',
              'trunk_free_begin', 'trunk_free_end', 'first_step_submit',
              'first_step_wait_begin', 'first_step_wait_end',
              'first_content_write_begin', 'first_content_write_end',
              'request_end')
HOST_CHUNK = ('main_chunk_begin', 'main_chunk_submit_end', 'main_chunk_complete')
# Non-overlapping host windows only. trunk_alloc and first_step_wait are nested
# details, deliberately excluded from this partition's sum.
HOST_PARTITION = (
    ('timing_setup', 'timing_setup_begin', 'timing_setup_end'),
    ('trunk_prepare', 'trunk_prepare_begin', 'trunk_prepare_end'),
    ('main_lock_wait', 'main_lock_wait_begin', 'main_lock_acquired'),
    ('main_reset', 'main_reset_begin', 'main_reset_end'),
    ('main_prefill', 'main_prefill_begin', 'main_prefill_end'),
    ('prefill_argmax', 'prefill_argmax_begin', 'prefill_argmax_end'),
    ('rolling_trunk_alloc', 'rolling_trunk_alloc_begin', 'rolling_trunk_alloc_end'),
    ('init_lock_wait', 'init_lock_wait_begin', 'init_lock_acquired'),
    ('draft_reset', 'draft_reset_begin', 'draft_reset_end'),
    ('shift', 'shift_begin', 'shift_end'),
    ('extend', 'extend_begin', 'extend_end'),
    ('checkpoint_reserve', 'checkpoint_reserve_begin', 'checkpoint_reserve_end'),
    ('scratch_reserve', 'scratch_reserve_begin', 'scratch_reserve_end'),
    ('trunk_free', 'trunk_free_begin', 'trunk_free_end'),
    ('first_step_dispatch_wait', 'first_step_submit', 'first_step_wait_end'),
    ('first_content_write', 'first_content_write_begin', 'first_content_write_end'))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value, label):
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
            f'{label}: expected finite nonnegative number')
    return value


def integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum,
            f'{label}: expected integer >= {minimum}')
    return value


def ulp32(value):
    """One full binary32 ULP, fixed before any event measurements are read."""
    number(value, 'CUDA float milliseconds')
    require(value <= float.fromhex('0x1.fffffep+127'), 'CUDA float overflow')
    rounded = struct.unpack('<f', struct.pack('<f', value))[0]
    require(rounded == value, 'CUDA elapsed value is not a recovered binary32')
    return math.ldexp(1.0, max(-149, math.frexp(value)[1] - 24)) if value else math.ldexp(1.0, -149)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f'duplicate JSON key: {key}')
        result[key] = value
    return result


def decode_json(text):
    def invalid(value):
        raise ValueError(f'nonstandard JSON constant: {value}')
    return json.loads(text, object_pairs_hook=unique_object,
                      parse_constant=invalid)


def trace_records(log):
    result = []
    for line in log.splitlines():
        if '[q4t][mtp_init_timing]' not in line:
            continue
        require(line.startswith(PREFIX), 'malformed timing record prefix')
        item = decode_json(line[len(PREFIX):])
        require(isinstance(item, dict), 'timing record is not an object')
        result.append(item)
    return result


def validate_stage(stage, name, skipped, label):
    require(isinstance(stage, dict) and stage.get('name') == name,
            f'{label}: missing or misplaced {name} stage')
    for key in ('recorded', 'ready'):
        require(stage.get(key) is True, f'{label}/{name}: {key} is not true')
    require(stage.get('skipped') is skipped, f'{label}/{name}: skipped mismatch')
    begin = number(stage.get('begin_host_ms'), f'{label}/{name}/begin')
    end = number(stage.get('end_host_ms'), f'{label}/{name}/end')
    require(begin <= end, f'{label}/{name}: reversed host markers')
    if skipped:
        require(stage.get('stream_ms') is None,
                f'{label}/{name}: skipped interval must be null')
    else:
        number(stage.get('stream_ms'), f'{label}/{name}/stream')
    return begin, end


def validate_trace(trace, response):
    """Strict schema v1 validation for one successful matrix request."""
    rid = response['response_id']
    for key, expected in [('schema_version', 1), ('response_id', rid),
                          ('prompt_tokens', response['actual_input']),
                          ('chunk_size', 8192), ('max_seq', 1),
                          ('text_only', True), ('complete', True),
                          ('valid', True), ('error', ''),
                          ('diagnostic_only', True),
                          ('stream_time_is_gpu_active', False),
                          ('host_and_stream_times_additive', False),
                          ('host_origin', 'handle_chat_entry'), ('unit', 'ms')]:
        require(trace.get(key) == expected and type(trace.get(key)) is type(expected),
                f'{rid}: invalid {key}: {trace.get(key)!r}')
    tokens = integer(trace['prompt_tokens'], 'prompt_tokens', 1)
    require(tokens in LENGTHS, f'{rid}: unfrozen prompt length')
    require(trace.get('trunk_bytes') == tokens * 10240 * 2 and
            type(trace.get('trunk_bytes')) is int, f'{rid}: trunk bytes mismatch')
    for key in ('timing_setup_host_ms', 'cuda_record_host_ms', 'report_host_ms'):
        number(trace.get(key), f'{rid}/{key}')
    chunk_shape = [(base, min(8192, tokens - base))
                   for base in range(0, tokens, 8192)]
    expected_marks = [(name, -1, 0) for name in HOST_BEFORE]
    expected_marks += [(name, base, rows) for base, rows in chunk_shape
                       for name in HOST_CHUNK]
    expected_marks += [(name, -1, 0) for name in HOST_AFTER]
    marks = trace.get('host_marks')
    require(isinstance(marks, list) and len(marks) == len(expected_marks),
            f'{rid}: host marker count mismatch')
    singles, main_chunks, previous = {}, [], 0.0
    for mark, expected in zip(marks, expected_marks):
        require(isinstance(mark, dict), f'{rid}: invalid host marker')
        require((mark.get('name'), mark.get('base'), mark.get('rows')) == expected,
                f'{rid}: host marker order/coverage mismatch, expected {expected}')
        require(type(mark.get('base')) is int and type(mark.get('rows')) is int,
                f'{rid}: invalid marker shape type')
        at = number(mark.get('at_ms'), f'{rid}/{expected[0]}')
        require(at >= previous, f'{rid}: host clock moved backwards')
        previous = at
        if expected[1] < 0:
            singles[expected[0]] = at
        elif expected[0] == 'main_chunk_begin':
            main_chunks.append({'base': expected[1], 'rows': expected[2],
                                'begin_ms': at})
        elif expected[0] == 'main_chunk_submit_end':
            main_chunks[-1]['submit_end_ms'] = at
        else:
            main_chunks[-1]['complete_ms'] = at
    outer = trace.get('outer')
    require(isinstance(outer, list) and len(outer) == 2,
            f'{rid}: exactly two outer event intervals required')
    outer_stream = {}
    for stage, name in zip(outer, ('main_prefill', 'draft_reset')):
        begin, end = validate_stage(stage, name, False, rid + '/outer')
        require(singles[name + '_begin'] <= begin <= end <= singles[name + '_end'],
                f'{rid}/{name}: outer record timestamps outside host scope')
        outer_stream[name] = stage['stream_ms']
    chunks = trace.get('chunks')
    require(isinstance(chunks, list) and len(chunks) == len(chunk_shape),
            f'{rid}: draft chunk count mismatch')
    modules = {name: [] for name in STAGES}
    chunk_results = []
    previous_chunk_end = singles['extend_alloc_end']
    for index, (chunk, shape) in enumerate(zip(chunks, chunk_shape)):
        require(isinstance(chunk, dict), f'{rid}: invalid draft chunk')
        require((chunk.get('base'), chunk.get('rows')) == shape and
                type(chunk.get('base')) is int and type(chunk.get('rows')) is int,
                f'{rid}: draft chunk coverage mismatch at {index}')
        final = index == len(chunks) - 1
        require(chunk.get('compute_logits') is final and
                chunk.get('logits_rows') == 'last',
                f'{rid}: head policy/last-chunk mismatch at {index}')
        stages = chunk.get('stages')
        require(isinstance(stages, list) and len(stages) == len(STAGES),
                f'{rid}: stage count mismatch at chunk {index}')
        previous_end = None
        durations = {}
        for stage, name in zip(stages, STAGES):
            skipped = name == 'head' and not final
            begin, end = validate_stage(stage, name, skipped,
                                        f'{rid}/chunk{index}')
            require(singles['extend_alloc_end'] <= begin <= end <= singles['extend_finalize_begin'],
                    f'{rid}: CUDA record timestamps outside extend scope')
            if previous_end is not None:
                require(begin == previous_end,
                        f'{rid}: stage boundaries not shared at chunk {index}/{name}')
            else:
                require(begin >= previous_chunk_end,
                        f'{rid}: chunk record windows overlap/reverse')
            previous_end = end
            durations[name] = stage['stream_ms']
            if not skipped:
                modules[name].append(stage['stream_ms'])
        previous_chunk_end = previous_end
        total = number(chunk.get('stream_total_ms'), f'{rid}/chunk{index}/total')
        gap = number(chunk.get('skipped_marker_gap_ms'), f'{rid}/chunk{index}/gap')
        require(not final or gap == 0, f'{rid}: final head has a skipped gap')
        leaves = [value for value in durations.values() if value is not None]
        leaf_total = math.fsum(leaves)
        residual = total - math.fsum(leaves + [gap])
        # Shared event endpoints telescope before float conversion. Budget one
        # full float32 ULP per CUDA elapsed result (more than 0.5 ULP RNE),
        # including independent whole-chunk and skipped-gap measurements.
        tolerance = math.fsum(ulp32(value) for value in leaves + [gap, total])
        require(abs(residual) <= tolerance,
                f'{rid}/chunk{index}: stream sum does not close within frozen ULP budget')
        chunk_results.append({'base': shape[0], 'rows': shape[1],
                              'compute_logits': final,
                              'stream_stage_ms': durations,
                              'covered_leaf_stream_ms': leaf_total,
                              'stream_total_ms': total,
                              'skipped_marker_gap_ms': gap,
                              'stream_closure_residual_ms': residual,
                              'stream_closure_ulp_budget_ms': tolerance,
                              'record_host_window_ms': previous_end -
                              stages[0]['begin_host_ms']})
    wall, gaps, previous_end = {}, [], 0.0
    for name, begin_name, end_name in HOST_PARTITION:
        begin, end = singles[begin_name], singles[end_name]
        require(previous_end <= begin <= end,
                f'{rid}: host partition overlaps at {name}')
        gaps.append(begin - previous_end)
        wall[name] = end - begin
        previous_end = end
    content_end = singles['first_content_write_end']
    gaps.append(content_end - previous_end)
    # Residual is the sum of nonnegative uncovered host gaps, not wall-GPU.
    covered_wall = math.fsum(wall.values())
    unassigned_wall = math.fsum(gaps)
    nested = {
        'trunk_alloc_ms': singles['trunk_alloc_end'] - singles['trunk_alloc_begin'],
        'first_step_wait_ms': singles['first_step_wait_end'] - singles['first_step_wait_begin'],
        'mtp_setup_through_trunk_free_ms': singles['trunk_free_end'] - singles['rolling_trunk_alloc_begin'],
        'request_end_ms': singles['request_end'],
        'first_content_write_begin_ms': singles['first_content_write_begin'],
    }
    extend_partition = {
        'allocation': singles['extend_alloc_end'] - singles['extend_alloc_begin'],
        'chunk_calls': singles['extend_finalize_begin'] - singles['extend_alloc_end'],
        'finalize_readback': singles['extend_cleanup_begin'] - singles['extend_finalize_begin'],
        'cleanup': singles['extend_cleanup_end'] - singles['extend_cleanup_begin'],
    }
    extend_gaps = ((singles['extend_alloc_begin'] - singles['extend_begin']) +
                   (singles['extend_end'] - singles['extend_cleanup_end']))
    return {'response_id': rid, 'prompt_tokens': tokens,
            'host_partition_ms': wall, 'host_nested_details_ms': nested,
            'entry_to_first_content_end_ms': content_end,
            'covered_host_partition_ms': covered_wall,
            'unassigned_host_gaps_ms': unassigned_wall,
            'host_partition_roundoff_ms': content_end - covered_wall - unassigned_wall,
            'timing_setup_host_ms': trace['timing_setup_host_ms'],
            'cuda_record_host_ms': trace['cuda_record_host_ms'],
            'report_host_ms': trace['report_host_ms'],
            'extend_nested_partition_ms': extend_partition,
            'extend_unassigned_host_gaps_ms': extend_gaps,
            'main_chunks_host': main_chunks,
            'outer_stream_ms': outer_stream,
            'draft_module_stream_ms': {name: math.fsum(values)
                                       for name, values in modules.items()},
            'draft_covered_leaf_stream_ms': math.fsum(
                value for values in modules.values() for value in values),
            'draft_chunks': chunk_results,
            'gpu_parent_plus_child_sum_performed': False,
            'host_plus_gpu_sum_performed': False}


def audit_traces(log, responses, enabled):
    records = trace_records(log)
    if not enabled:
        require(not records, 'diagnostics-disabled run emitted timing records')
        return []
    expected_ids = [r['response_id'] for r in responses]
    require(len(expected_ids) == len(set(expected_ids)), 'duplicate response IDs')
    by_id = {}
    for record in records:
        rid = record.get('response_id')
        require(isinstance(rid, str) and rid not in by_id,
                f'missing/duplicate timing response ID: {rid!r}')
        by_id[rid] = record
    require(set(by_id) == set(expected_ids), 'timing records do not cover HTTP IDs')
    return [validate_trace(by_id[row['response_id']], row) for row in responses]


class Reader:
    def __init__(self):
        self.sources = {}

    def bind(self, path):
        path = Path(path).resolve()
        content = path.read_bytes()
        identity = {'bytes': len(content),
                    'sha256': hashlib.sha256(content).hexdigest()}
        old = self.sources.setdefault(str(path), identity)
        require(old == identity, f'source changed while auditing: {path}')
        return content

    def json(self, path):
        return decode_json(self.bind(path).decode())


def read_raw_case(reader, case):
    """Match runner JSON to actual trusted-local SQLite response payloads."""
    exported = reader.json(case / 'responses.json')
    databases = list(case.rglob('benchmark_data.db'))
    require(len(databases) == 1, f'{case}: expected one result database')
    database = databases[0].resolve()
    reader.bind(database)
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        rows = db.execute(
            'select success,prompt_tokens,completion_tokens,response_messages,'
            'first_chunk_latency,latency,request from result order by start_time'
        ).fetchall()
    require(isinstance(exported, list) and len(exported) == len(rows),
            f'{case}: exported/raw response count mismatch')
    parsed = []
    for index, (row, saved) in enumerate(zip(rows, exported)):
        messages = pickle.loads(base64.b64decode(row[3]))
        identity = response_identity(messages)
        require(identity['response_id_valid'], f'{case}/{index}: invalid raw ID')
        choices = [choice for message in messages
                   for choice in message.get('choices', [])]
        request = decode_json(row[6])
        prompt = request['prompt']
        require(isinstance(prompt, str), f'{case}/{index}: prompt is not text')
        current = {'success': row[0], 'actual_input': row[1], 'actual_output': row[2],
                   'text': ''.join(c.get('delta', c.get('message', {})).get('content', '')
                                   for c in choices),
                   'finish': [c['finish_reason'] for c in choices if c.get('finish_reason')],
                   'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                   'ttft': row[4], 'latency': row[5], 'decode_mode': 'mtp',
                   'request_stream': request.get('stream'), **identity}
        for key, value in current.items():
            require(saved.get(key) == value, f'{case}/{index}: raw/export {key} differs')
        require(current['success'] == 1, f'{case}/{index}: failed HTTP request')
        current['text_sha256'] = hashlib.sha256(current['text'].encode()).hexdigest()
        current['repeat'] = index
        parsed.append(current)
    reader.bind(database)  # Reject an evidence DB changing during the audit.
    return parsed


def load_run(reader, path, kind, enabled):
    path = Path(path).resolve()
    mode = reader.json(path / 'run-mode.json')
    require(mode.get('decode_mode') == 'mtp' and mode.get('acceptance_mode') == kind,
            f'{path}: requested mode mismatch')
    require(mode.get('capacity') == {'max_seq': 1, 'max_prefill': 8192,
                                     'max_len': 208896}, f'{path}: capacity changed')
    exit_result = reader.json(path / 'exit.json')
    require(exit_result.get('server') == 0 and exit_result.get('failure') is None and
            exit_result.get('actual_mode_checks_passed') is True and
            exit_result.get('http_output_checks_passed') is True and
            exit_result.get('completed') == (11 if kind == 'quality' else 5),
            f'{path}: run did not complete cleanly')
    command = reader.json(path / 'server-command.json')
    require(command.get('decode_mode') == 'mtp' and '--mtp' in command['argv'] and
            '--no-mtp' not in command['argv'], f'{path}: invalid server command')
    log = reader.bind(path / 'server.log').decode()
    startup = startup_evidence(log, True)
    require(startup['passed'], f'{path}: startup: {startup["errors"]}')
    cases = [path / 'quality'] if kind == 'quality' else [
        path / f'context-{length}' for length in LENGTHS]
    responses, per_length = [], {}
    for case in cases:
        rows = read_raw_case(reader, case)
        require(len(rows) == (11 if kind == 'quality' else 3),
                f'{case}: request count mismatch')
        if kind == 'performance':
            length = int(case.name.split('-')[1])
            for row in rows:
                require(row['actual_input'] == length and row['actual_output'] == 256 and
                        row['finish'] == ['length'] and row['request_stream'] is True,
                        f'{case}: shape/finish/stream mismatch')
                row['client_metrics'] = performance_metrics(row)
            require(len({r['prompt_sha256'] for r in rows}) == 1,
                    f'{case}: repeated prompts differ')
            per_length[length] = rows
        responses.extend(rows)
    paths = request_mode_evidence(log, responses, True)
    require(paths['passed'], f'{path}: request paths: {paths["errors"]}')
    for row, binding in zip(responses, paths['bindings']):
        row['actual_path'] = binding['records'][0]['fields']
    traced = audit_traces(log, responses, enabled)
    return {'directory': str(path), 'mode': kind, 'enabled': enabled,
            'binary_path': mode['binary'], 'server_argv': command['argv'],
            'responses': responses, 'per_length': per_length, 'traces': traced}


def summarize_client(rows):
    metrics = [row['client_metrics'] for row in rows]
    return {'ttft_mean_s': statistics.mean(m['ttft'] for m in metrics),
            'latency_mean_s': statistics.mean(m['latency'] for m in metrics),
            'decode_tps_aggregate': sum(m['actual_output'] - 1 for m in metrics) /
            math.fsum(m['decode_seconds'] for m in metrics),
            'overall_tps_aggregate': sum(m['actual_output'] for m in metrics) /
            math.fsum(m['latency'] for m in metrics),
            'first': metrics[0], 'later': metrics[1:], 'all': metrics}


def compare_runs(left, right, label):
    differences, tiers = [], []
    for length in LENGTHS:
        a, b = left['per_length'][length], right['per_length'][length]
        pairs = []
        for i, (x, y) in enumerate(zip(a, b)):
            fields = ('prompt_sha256', 'actual_input', 'actual_output', 'text', 'finish')
            changed = [key for key in fields if x[key] != y[key]]
            for key in ('requested_mtp', 'path', 'mtp_steps', 'fallback', 'plain_tail_tokens'):
                if x['actual_path'][key] != y['actual_path'][key]:
                    changed.append('actual_path.' + key)
            if changed:
                differences.append({'length': length, 'repeat': i, 'fields': changed})
            pairs.append({'repeat': i, 'left_response_id': x['response_id'],
                          'right_response_id': y['response_id'],
                          'left_output_sha256': x['text_sha256'],
                          'right_output_sha256': y['text_sha256'],
                          'same_output_and_path': not changed})
        am, bm = summarize_client(a), summarize_client(b)
        ratios = {key: bm[key] / am[key] - 1 for key in
                  ('ttft_mean_s', 'latency_mean_s', 'decode_tps_aggregate',
                   'overall_tps_aggregate')}
        tiers.append({'length': length, 'left': am, 'right': bm,
                      'right_relative_change': ratios, 'pairs': pairs})
    return {'label': label, 'output_and_path_equal': not differences,
            'differences': differences, 'tiers': tiers,
            'performance_acceptance_or_causal_zero_overhead_claim': False}


def analyze(quality, control, diagnostic, historical):
    reader = Reader()
    q = load_run(reader, quality, 'quality', False)
    c = load_run(reader, control, 'performance', False)
    d = load_run(reader, diagnostic, 'performance', True)
    h = load_run(reader, historical, 'performance', False)
    require(q['binary_path'] == c['binary_path'] == d['binary_path'],
            'new quality/control/diagnostic binary paths differ')
    comparisons = [compare_runs(c, d, 'same-build control -> diagnostic'),
                   compare_runs(h, c, 'historical candidate -> new disabled'),
                   compare_runs(h, d, 'historical candidate -> new diagnostic')]
    traces_by_id = {t['response_id']: t for t in d['traces']}
    tiers = []
    for length in LENGTHS:
        runs = [traces_by_id[r['response_id']] for r in d['per_length'][length]]
        tiers.append({'length': length, 'requests': runs,
                      'host_partition_mean_ms': {
                          name: statistics.mean(r['host_partition_ms'][name] for r in runs)
                          for name, _, _ in HOST_PARTITION},
                      'draft_module_stream_mean_ms': {
                          name: statistics.mean(r['draft_module_stream_ms'][name] for r in runs)
                          for name in STAGES}})
    return {'schema_version': 1,
            'passed': all(x['output_and_path_equal'] for x in comparisons),
            'coverage': {'quality_off': 11, 'control_off': 15, 'diagnostic_on': 15,
                         'new_http_total': 41, 'historical_auxiliary': 15,
                         'complete_valid_diagnostic_records': len(d['traces'])},
            'comparisons': comparisons, 'timing_tiers': tiers,
            'sources': reader.sources,
            'limits': [
                'Runtime/device-code identity is reviewed separately; matching HTTP text is not a numerical oracle.',
                'Host wall includes existing waits; CUDA event intervals include stream idle/host submission gaps and are not kernel active time.',
                'Only non-overlapping draft leaf event intervals are summed. Outer intervals and host wall are not added to those sums.',
                'The fixed per-term binary32 ULP closure budget screens shared-boundary arithmetic consistency; it is not an absolute GPU timing-accuracy bound or an official CUDA guarantee.',
                'Host residual is uncovered host timeline gaps, never wall minus GPU.',
                'Client first_chunk_latency, HandleChat host clock and CUDA event clocks have distinct origins/coverage; no cross-clock subtraction is performed.',
                'Query/report/event-destruction work occurs after request_end. report_host_ms excludes final fprintf and is not complete observer overhead; setup/record/report costs are not added to the existing wall partition.',
                'Server ttft_seconds is recorded before MTP initialization and is not used as client TTFT.',
                'Control precedes diagnostics; three samples per tier are not randomized evidence of zero observer overhead or stable tail latency.',
                'Historical timing is auxiliary. This diagnostic does not change the prior performance or MTP enablement NO_GO.'
            ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('quality', 'control', 'diagnostic', 'historical', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    require(not args.output.exists(), 'refuse to overwrite analysis output')
    try:
        result = analyze(args.quality, args.control, args.diagnostic, args.historical)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        result = {'schema_version': 1, 'passed': False,
                  'error': str(error), 'analysis_incomplete': True}
    with args.output.open('x') as file:
        json.dump(result, file, indent=2, ensure_ascii=False, allow_nan=False)
        file.write('\n')
    print(json.dumps({'passed': result['passed'], 'output': str(args.output)}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
