"""Synthetic offline timing-parser contracts. No files, model or CUDA calls."""
import copy
import json
import struct
import unittest

from analyze_mtp_init_timing import (HOST_AFTER, HOST_BEFORE, HOST_CHUNK,
                                     LENGTHS, PREFIX, STAGES, audit_traces,
                                     compare_runs, trace_records, ulp32,
                                     validate_trace)


def response(tokens=1024, rid='r1'):
    return {'response_id': rid, 'actual_input': tokens}


def fixture(tokens=1024, rid='r1'):
    shapes = [(base, min(8192, tokens - base))
              for base in range(0, tokens, 8192)]
    names = [(name, -1, 0) for name in HOST_BEFORE]
    names += [(name, base, rows) for base, rows in shapes for name in HOST_CHUNK]
    names += [(name, -1, 0) for name in HOST_AFTER]
    marks = [{'name': name, 'base': base, 'rows': rows,
              'at_ms': float(index + 1) * 10}
             for index, (name, base, rows) in enumerate(names)]
    single = {m['name']: m['at_ms'] for m in marks if m['base'] < 0}
    def stage(name, begin, end, skipped=False):
        return {'name': name, 'begin_host_ms': begin, 'end_host_ms': end,
                'stream_ms': None if skipped else 0.25,
                'recorded': True, 'ready': True, 'skipped': skipped}
    outer = [stage(name, single[name + '_begin'] + 2,
                   single[name + '_end'] - 2)
             for name in ('main_prefill', 'draft_reset')]
    chunks = []
    lo, hi = single['extend_alloc_end'], single['extend_finalize_begin']
    for index, (base, rows) in enumerate(shapes):
        final = index + 1 == len(shapes)
        boundaries = [lo + (hi - lo) * (index * 10 + j + 0.5) /
                      (10 * len(shapes) + 1) for j in range(10)]
        stages = [stage(name, boundaries[j], boundaries[j + 1],
                        name == 'head' and not final)
                  for j, name in enumerate(STAGES)]
        gap = 0.0 if final else 0.125
        chunks.append({'base': base, 'rows': rows, 'compute_logits': final,
                       'logits_rows': 'last', 'stages': stages,
                       'stream_total_ms': 2.25 if final else 2.125,
                       'skipped_marker_gap_ms': gap})
    return {'schema_version': 1, 'response_id': rid, 'prompt_tokens': tokens,
            'chunk_size': 8192, 'max_seq': 1, 'text_only': True,
            'complete': True, 'valid': True, 'error': '',
            'diagnostic_only': True, 'host_origin': 'handle_chat_entry',
            'unit': 'ms', 'timing_setup_host_ms': 10.0,
            'cuda_record_host_ms': 0.25, 'report_host_ms': 0.5,
            'trunk_bytes': tokens * 10240 * 2,
            'stream_time_is_gpu_active': False,
            'host_and_stream_times_additive': False,
            'host_marks': marks, 'outer': outer, 'chunks': chunks}


def logged(trace):
    return PREFIX + json.dumps(trace) + '\n'


def run_fixture(label):
    by_length = {}
    for length in LENGTHS:
        rows = []
        for index in range(3):
            rows.append({'response_id': f'{label}-{length}-{index}',
                         'prompt_sha256': f'prompt-{length}',
                         'actual_input': length, 'actual_output': 256,
                         'text': 'same text', 'text_sha256': 'same-hash',
                         'finish': ['length'],
                         'actual_path': {'requested_mtp': '1',
                                         'path': 'mtp_multi_b1', 'mtp_steps': '80',
                                         'fallback': 'none', 'plain_tail_tokens': '0'},
                         'client_metrics': {'ttft': 1.0, 'latency': 11.0,
                                            'decode_seconds': 10.0,
                                            'decode_tps': 25.5,
                                            'overall_tps': 256 / 11,
                                            'actual_output': 256}})
        by_length[length] = rows
    return {'per_length': by_length}


class TimingSchemaTest(unittest.TestCase):
    def assert_invalid(self, trace, tokens=1024):
        with self.assertRaises(ValueError):
            validate_trace(trace, response(tokens))

    def test_all_five_topologies_are_complete(self):
        for tokens in LENGTHS:
            with self.subTest(tokens=tokens):
                result = validate_trace(fixture(tokens), response(tokens))
                self.assertEqual(len(result['draft_chunks']), (tokens - 1) // 8192 + 1)
                self.assertEqual(sum(c['rows'] for c in result['draft_chunks']), tokens)
                self.assertFalse(result['host_plus_gpu_sum_performed'])
                self.assertFalse(result['gpu_parent_plus_child_sum_performed'])

    def test_host_partition_counts_nested_scopes_once(self):
        result = validate_trace(fixture(), response())
        self.assertNotIn('trunk_alloc', result['host_partition_ms'])
        self.assertNotIn('first_step_wait', result['host_partition_ms'])
        self.assertAlmostEqual(result['covered_host_partition_ms'] +
                               result['unassigned_host_gaps_ms'],
                               result['entry_to_first_content_end_ms'])
        self.assertAlmostEqual(sum(result['extend_nested_partition_ms'].values()) +
                               result['extend_unassigned_host_gaps_ms'],
                               result['host_partition_ms']['extend'])

    def test_gpu_skipped_marker_gap_is_not_head_work(self):
        result = validate_trace(fixture(45056), response(45056))
        self.assertEqual(result['draft_module_stream_ms']['head'], 0.25)
        for chunk in result['draft_chunks'][:-1]:
            self.assertIsNone(chunk['stream_stage_ms']['head'])
            self.assertEqual(chunk['skipped_marker_gap_ms'], 0.125)
            self.assertEqual(chunk['stream_total_ms'], 2.125)
            self.assertEqual(chunk['covered_leaf_stream_ms'], 2.0)

    def test_disabled_means_no_trace_and_no_required_trace(self):
        self.assertEqual(audit_traces('[q4t] ordinary log\n', [response()], False), [])
        with self.assertRaises(ValueError):
            audit_traces(logged(fixture()), [response()], False)

    def test_request_ids_bind_independently_of_log_order(self):
        log = logged(fixture(rid='second')) + logged(fixture(rid='first'))
        result = audit_traces(log, [response(rid='first'), response(rid='second')], True)
        self.assertEqual([r['response_id'] for r in result], ['first', 'second'])

    def test_missing_extra_duplicate_trace_and_duplicate_http_ids_rejected(self):
        valid = logged(fixture())
        for log, rows in [('', [response()]), (valid + valid, [response()]),
                          (valid, [response(rid='other')]),
                          (valid, [response(), response()])]:
            with self.subTest(log=log[:40]), self.assertRaises(ValueError):
                audit_traces(log, rows, True)

    def test_duplicate_json_keys_and_partial_lines_rejected(self):
        duplicate = logged(fixture()).replace('"schema_version": 1',
                                              '"schema_version": 1, "schema_version": 1')
        for log in [duplicate, 'prefix ' + logged(fixture()),
                    PREFIX + '{', PREFIX + '[]\n']:
            with self.subTest(log=log[:70]), self.assertRaises(ValueError):
                trace_records(log)

    def test_invalid_scope_status_and_shape_rejected(self):
        for key, value in [('schema_version', 2), ('schema_version', True),
                           ('complete', False), ('valid', False),
                           ('error', 'event_not_ready'), ('unit', 's'),
                           ('host_origin', 'request_receive'), ('max_seq', 2),
                           ('text_only', False), ('trunk_bytes', 4),
                           ('stream_time_is_gpu_active', True),
                           ('host_and_stream_times_additive', True)]:
            trace = fixture()
            trace[key] = value
            with self.subTest(key=key):
                self.assert_invalid(trace)

    def test_nonfinite_negative_and_wrong_numeric_types_rejected(self):
        for value in [float('nan'), float('inf'), -1.0, True, None, '1']:
            trace = fixture()
            trace['chunks'][0]['stages'][0]['stream_ms'] = value
            with self.subTest(value=value):
                self.assert_invalid(trace)
        for key in ['timing_setup_host_ms', 'cuda_record_host_ms', 'report_host_ms']:
            trace = fixture()
            trace[key] = -1
            self.assert_invalid(trace)

    def test_unready_unrecorded_and_missing_event_rejected(self):
        for key in ['ready', 'recorded']:
            trace = fixture()
            trace['chunks'][0]['stages'][2][key] = False
            self.assert_invalid(trace)
        trace = fixture()
        trace['chunks'][0]['stages'].pop()
        self.assert_invalid(trace)
        trace = fixture()
        trace['outer'].pop()
        self.assert_invalid(trace)

    def test_main_and_draft_chunk_gaps_duplicates_and_missing_rows_rejected(self):
        for change in ['base', 'rows', 'duplicate', 'missing']:
            trace = fixture(45056)
            if change == 'base':
                trace['chunks'][1]['base'] += 1
            elif change == 'rows':
                trace['chunks'][0]['rows'] -= 1
            elif change == 'duplicate':
                trace['chunks'][1] = copy.deepcopy(trace['chunks'][0])
            else:
                trace['chunks'].pop()
            self.assert_invalid(trace, 45056)
        trace = fixture()
        mark = next(m for m in trace['host_marks'] if m['name'] == 'main_chunk_complete')
        mark['rows'] -= 1
        self.assert_invalid(trace)

    def test_head_coverage_must_match_final_chunk(self):
        for change in ['early', 'missing', 'null', 'wrong_mode', 'gap']:
            trace = fixture(45056)
            if change == 'early':
                trace['chunks'][0]['compute_logits'] = True
            elif change == 'missing':
                trace['chunks'][-1]['compute_logits'] = False
            elif change == 'null':
                trace['chunks'][-1]['stages'][-1]['stream_ms'] = None
            elif change == 'wrong_mode':
                trace['chunks'][-1]['logits_rows'] = 'all'
            else:
                trace['chunks'][-1]['skipped_marker_gap_ms'] = 0.125
            self.assert_invalid(trace, 45056)

    def test_missing_skipped_gap_is_not_silently_zero(self):
        trace = fixture(45056)
        del trace['chunks'][0]['skipped_marker_gap_ms']
        self.assert_invalid(trace, 45056)

    def test_shared_boundaries_and_host_monotonic_order(self):
        trace = fixture()
        trace['chunks'][0]['stages'][2]['begin_host_ms'] += 0.01
        self.assert_invalid(trace)
        trace = fixture()
        trace['host_marks'][1]['at_ms'] = 0
        self.assert_invalid(trace)
        trace = fixture()
        trace['host_marks'][1], trace['host_marks'][2] = trace['host_marks'][2], trace['host_marks'][1]
        self.assert_invalid(trace)

    def test_outer_records_must_belong_to_declared_host_scope(self):
        trace = fixture()
        trace['outer'][0]['begin_host_ms'] = 0
        self.assert_invalid(trace)

    def test_zero_leaf_time_can_be_real_but_not_missing(self):
        trace = fixture()
        trace['chunks'][0]['stages'][0]['stream_ms'] = 0.0
        trace['chunks'][0]['stream_total_ms'] -= 0.25
        validate_trace(trace, response())

    def test_predeclared_ulp_budget_keeps_signed_residual(self):
        trace = fixture()
        original = trace['chunks'][0]['stream_total_ms']
        bits = struct.unpack('<I', struct.pack('<f', original))[0]
        trace['chunks'][0]['stream_total_ms'] = struct.unpack('<f', struct.pack('<I', bits - 1))[0]
        result = validate_trace(trace, response())
        chunk = result['draft_chunks'][0]
        self.assertLess(chunk['stream_closure_residual_ms'], 0)
        self.assertLessEqual(abs(chunk['stream_closure_residual_ms']),
                             chunk['stream_closure_ulp_budget_ms'])
        self.assertEqual(ulp32(original), original - trace['chunks'][0]['stream_total_ms'])

    def test_bad_stream_closure_rejected_without_empirical_slack(self):
        trace = fixture()
        trace['chunks'][0]['stream_total_ms'] += 0.25
        self.assert_invalid(trace)
        trace = fixture(45056)
        trace['chunks'][0]['skipped_marker_gap_ms'] = 0.0
        self.assert_invalid(trace, 45056)

    def test_outputs_compare_across_distinct_process_ids(self):
        result = compare_runs(run_fixture('old'), run_fixture('new'), 'pair')
        self.assertTrue(result['output_and_path_equal'])
        self.assertFalse(result['performance_acceptance_or_causal_zero_overhead_claim'])

    def test_output_and_execution_path_changes_are_preserved(self):
        for key in ['text', 'actual_output', 'finish', 'mtp_steps']:
            a, b = run_fixture('old'), run_fixture('new')
            row = b['per_length'][1024][0]
            if key == 'mtp_steps':
                row['actual_path'][key] = '79'
            else:
                row[key] = {'text': 'different', 'actual_output': 255,
                            'finish': ['stop']}[key]
            result = compare_runs(a, b, 'pair')
            self.assertFalse(result['output_and_path_equal'])
            self.assertEqual(len(result['differences']), 1)


if __name__ == '__main__':
    unittest.main()
