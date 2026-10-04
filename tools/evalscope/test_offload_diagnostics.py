"""Synthetic diagnostic evidence attacks; no service, GPU or model access."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import audit_offload_diagnostics as audit


def cache_layer(layer):
    return {'layer': layer, 'slot_experts': list(range(256)),
            'slot_ticks': list(range(256)), 'slot_protected': [0] * 256,
            'l2_experts': list(range(16)), 'l2_ticks': list(range(16)),
            'mirror_experts': list(range(8)), 'slot_clock': 256,
            'l2_clock': 16, 'mirror_cursor': 0}


def fixture(output=256):
    forwards = output - 1
    events = [('prefill_begin', 0), ('prefill_end_decode_begin', 0),
              *[('decode_prefix', n) for n in (1, 8, 32) if n <= forwards],
              ('inference_end', forwards)]
    snapshots = []
    for index, (event, count) in enumerate(events):
        start = 2_000_000_000 + index * 100_000_000
        full = event != 'decode_prefix'
        snapshots.append({'event': event, 'decode_forward_count': count,
            'monotonic_ns': start, 'realtime_ns': start + 100_000_000_000,
            'capture_end_monotonic_ns': start + 100,
            'capture_end_realtime_ns': start + 100_000_000_100,
            'capture_ns': 100, 'pid_read_bytes': (1 << 54) + index * 4096,
            'pid_read_bytes_scope': 'process_all_files',
            'gpu_work_complete': True,
            'cache_state': 'full' if full else 'omitted',
            'stats': {key: index * 10 for key in audit.STAT_KEYS},
            'timing': {'enabled': True,
                       **{key: index * 100 for key in audit.TIMING_KEYS}},
            'layers': [cache_layer(layer) for layer in range(48)]
                      if full else None})
    record = {'schema': audit.SCHEMA, 'request_id': 'chatcmpl-auto-1',
              'outcome': 'success', 'completeness': 'complete',
              'input_tokens': 1024, 'output_tokens': output,
              'decode_forwards_completed': forwards, 'snapshots': snapshots}
    response = {'response_id': 'chatcmpl-auto-1', 'response_id_valid': True,
                'actual_input': 1024, 'actual_output': output,
                'prompt_sha256': 'a' * 64, 'text': 'fixed output',
                'ttft': 1.1, 'latency': 3.,
                'timing': {'within_client_boundaries': True,
                           'start_monotonic_seconds': 1.,
                           'end_monotonic_seconds': 4.}}
    return record, response


class PhaseContracts(unittest.TestCase):
    def test_complete_record_joins_actual_ids_and_uses_server_phase(self):
        record, response = fixture()
        result = audit.audit_record(record, response)
        self.assertEqual(result['response_id'], response['response_id'])
        self.assertEqual(result['prefill']['stats']['loads'], 10)
        self.assertEqual(result['decode']['stats']['loads'], 40)
        self.assertEqual(result['decode']['pid_read_bytes'], 4 * 4096)
        self.assertEqual(result['capture_ns_total'], 600)
        self.assertEqual([row['decode_forward_count'] for row in
                          result['decode_prefixes']], [1, 8, 32])

    def test_short_quality_outputs_have_only_reached_prefixes(self):
        record, response = fixture(7)
        result = audit.audit_record(record, response)
        self.assertEqual(len(result['decode_prefixes']), 1)
        self.assertEqual(result['actual_output'], 7)

    def test_unknown_pid_io_is_retained_without_invented_zero(self):
        record, response = fixture()
        record['snapshots'][1]['pid_read_bytes'] = None
        result = audit.audit_record(record, response)
        self.assertIsNone(result['decode']['pid_read_bytes'])
        self.assertEqual(result['decode']['pid_read_status'],
                         'unknown_or_counter_decreased')

    def test_cumulative_maximum_is_not_subtracted_as_interval_max(self):
        record, response = fixture()
        result = audit.audit_record(record, response)
        timing = result['decode']['timing']
        self.assertNotIn('stage_max_ns', timing['count_and_sum_deltas'])
        self.assertEqual(timing['cumulative_maxima_at_end']['stage_max_ns'], 500)

    def test_double_byte_sources_allowed_but_integer_counters_strict(self):
        record, response = fixture()
        for snapshot in record['snapshots']:
            snapshot['stats']['load_bytes'] = float(snapshot['stats']['load_bytes'])
        audit.audit_record(record, response)
        for bad in (True, 0.5, float('nan'), -1):
            altered = copy.deepcopy(record)
            altered['snapshots'][0]['stats']['loads'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                audit.audit_record(altered, response)

    def test_failed_partial_count_or_id_evidence_rejected(self):
        record, response = fixture()
        for change in ({'outcome': 'cancelled'}, {'outcome': 'failed'},
                       {'completeness': 'partial'}, {'request_id': 'other'},
                       {'input_tokens': 4096}, {'output_tokens': 255},
                       {'decode_forwards_completed': 256}, {'schema': 'unknown'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                audit.audit_record(dict(record, **change), response)

    def test_missing_duplicate_or_unsynchronized_phases_rejected(self):
        record, response = fixture()
        cases = []
        changed = copy.deepcopy(record)
        changed['snapshots'].pop(2)
        cases.append(changed)
        changed = copy.deepcopy(record)
        changed['snapshots'].insert(2, changed['snapshots'][1])
        cases.append(changed)
        for key, value in (('gpu_work_complete', False),
                           ('capture_ns', 0), ('realtime_ns', 0),
                           ('monotonic_ns', 0)):
            changed = copy.deepcopy(record)
            changed['snapshots'][1][key] = value
            cases.append(changed)
        for changed in cases:
            with self.assertRaises(ValueError):
                audit.audit_record(changed, response)

    def test_empty_missing_or_decreasing_counter_sets_rejected(self):
        record, response = fixture()
        for key, value in (('stats', {}), ('timing', {'enabled': True}),
                           ('timing', None), ('layers', [{}] * 48)):
            changed = copy.deepcopy(record)
            changed['snapshots'][1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                audit.audit_record(changed, response)
        record['snapshots'][-1]['stats']['loads'] = 0
        with self.assertRaisesRegex(ValueError, 'decreased'):
            audit.audit_record(record, response)

    def test_cache_dimensions_identity_ticks_and_protection_checked(self):
        record, response = fixture()
        for key, value in (('layer', 1), ('slot_experts', [0] * 256),
                ('slot_experts', [512] + list(range(1, 256))),
                ('slot_ticks', [-1] * 256), ('slot_ticks', [257] * 256),
                ('slot_protected', [2] * 256), ('l2_experts', [0] * 16),
                ('mirror_experts', [0] * 8), ('mirror_cursor', 8)):
            changed = copy.deepcopy(record)
            changed['snapshots'][1]['layers'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                audit.audit_record(changed, response)

    def test_empty_cache_slots_are_valid_and_not_fake_experts(self):
        record, response = fixture()
        layer = record['snapshots'][0]['layers'][0]
        layer['slot_experts'] = [-1] * 256
        layer['l2_experts'] = [-1] * 16
        layer['mirror_experts'] = [-1] * 8
        audit.audit_record(record, response)

    def test_phase_outside_actual_http_window_rejected(self):
        record, response = fixture()
        response['timing']['end_monotonic_seconds'] = 2.1
        with self.assertRaisesRegex(ValueError, 'outside HTTP'):
            audit.audit_record(record, response)

    def test_log_reader_preserves_large_uint_and_rejects_duplicate_ids(self):
        root = Path(__file__).resolve().parents[2] / '.q4t-work'
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            path = Path(temporary) / 'server.log'
            record, _ = fixture()
            line = audit.PREFIX + json.dumps(record) + '\n'
            path.write_text('ordinary log\n' + line)
            sources = {}
            records, _ = audit.read_phase_records(path, sources)
            self.assertEqual(records[record['request_id']]['snapshots'][0]
                             ['pid_read_bytes'], 1 << 54)
            self.assertIn(str(path), sources)
            path.write_text(line + line)
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                audit.read_phase_records(path, {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
