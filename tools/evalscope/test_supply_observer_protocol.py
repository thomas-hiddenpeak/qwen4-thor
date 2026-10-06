"""Synthetic supply contracts; no service, GPU, model, or old route replay."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import supply_observer_protocol as protocol


def document(index=1):
    condition, enabled = protocol.GROUP_ORDER[index - 1]
    length = 1024 if condition == 'AS' else 8193
    return {'schema': 1, 'scope': protocol.SCOPE,
        'phase_plan_sha256': 'a' * 64, 'trace_max_mib': 128,
        'group': {'id': f's{index:02d}-{condition.lower()}-'
                       + ('on' if enabled else 'off'),
            'index': index, 'pair_index': (index + 1) // 2,
            'pair_position': 1 + (index - 1) % 2, 'condition': condition,
            'arm': 'A', 'predecessor': condition[1],
            'predecessor_tokens': length, 'observation': 'on',
            'observer': enabled, 'requests': [
                {'position': n, 'role': 'conditioning' if n == 0 else 'probe',
                 'input_tokens': length if n == 0 else 1024, 'output_tokens': 256}
                for n in range(4)]}}


def witness(layer, seq, clock, state='active_reservation', final='published'):
    return {'layer': layer, 'plan_clock': clock, 'entry_index': 1,
        'expert': 300, 'source_task': 1, 'overwriting_task': 0,
        'GPU_victim': 8, 'mirror_slot': 0, 'entry_L2_present': False,
        'entry_mirror_candidate': True, 'reserve_seq': 1,
        'claim_seq': 3, 'publication_seq_or_zero':
            0 if final == 'aborted' else 2 if state == 'published' else 4,
        'state_at_claim': state, 'publication_final_outcome': final,
        'actual_source': 'READ', 'read_ok': True, 'commit_ok': True,
        'plan_complete': True, 'witness_seq': seq}


def observer(layer, decode, prefill_single, direct):
    c = dict.fromkeys(protocol.COUNTERS, 0)
    count = decode + int(prefill_single)
    for key in ('plans_started', 'plans_complete', 'planned_loads',
                'committed_loads', 'writeback_reservations', 'writeback_published'):
        c[key] = count
    c['source_read'] = decode
    if prefill_single:
        # Deliberately outside candidates: allowed only in singleton prefill.
        c['source_mirror'] = 1
        c['source_mirror_outside_entry_candidates'] = 1
        c['entry_l2_present'] = 1
        c['entry_mirror_present'] = 1
        c['entry_claimed_mirrors'] = 1
    samples = []
    if direct:
        for key in ('entry_mirror_present', 'entry_mirror_candidate',
                    'entry_candidate_to_read_direct_active',
                    'writeback_pending_missing_targets',
                    'writeback_pending_entry_candidate_targets',
                    'samples_confirmed_total'):
            c[key] += decode
        c['samples_overwritten'] = max(0, decode - 4)
        samples = [witness(layer, seq, 257 + int(prefill_single) + seq)
                   for seq in range(max(1, decode - 3), decode + 1)]
    return {'schema': protocol.SCHEMA, 'enabled': True, 'counters': c,
            'samples': samples, 'plan_state_bytes': 2048,
            'persistent_state_bytes': 1024}


def fixture(output=256, *, enabled=True, direct=False, prefill_single=False):
    forwards = output - 1
    events = [('prefill_begin', 0), ('prefill_end_decode_begin', 0),
              *[('decode_prefix', n) for n in (1, 8, 32) if n <= forwards],
              ('inference_end', forwards)]
    snapshots = []
    for index, (event, decode) in enumerate(events):
        started = index > 0
        single = bool(started and prefill_single)
        stats = dict.fromkeys(protocol.phase.STAT_KEYS, 0)
        if started:
            stats.update(resolve_calls=(1 + int(single) + decode) * 48,
                loads=(2 + int(single) + decode) * 48,
                misses=(2 + int(single) + decode) * 48,
                l2_misses=(2 + decode) * 48,
                shape_single_misses=(int(single) + decode) * 48,
                l2_shape_single_misses=decode * 48,
                shape_single_lookups=(int(single) + decode) * 480,
                mirror_hits=int(single) * 48,
                mirror_writebacks=(2 + int(single) + decode) * 48)
        layers = []
        if event != 'decode_prefix':
            for n in range(48):
                layer = {'layer': n, 'slot_experts': list(range(256)),
                    'slot_ticks': list(range(256)), 'slot_protected': [0] * 256,
                    'l2_experts': list(range(16)), 'l2_ticks': list(range(16)),
                    'mirror_experts': list(range(8)),
                    'slot_clock': 256 + (1 + int(single) + decode if started else 0),
                    'l2_clock': 16 + (2 + decode if started else 0), 'mirror_cursor': 0}
                if enabled:
                    layer['supply_observer'] = observer(n, decode, single, direct)
                layers.append(layer)
        start = 2_000_000_000 + index * 100_000_000
        snapshots.append({'event': event, 'decode_forward_count': decode,
            'monotonic_ns': start, 'realtime_ns': start + 100_000_000_000,
            'capture_end_monotonic_ns': start + 100,
            'capture_end_realtime_ns': start + 100_000_000_100, 'capture_ns': 100,
            'pid_read_bytes': (1 << 54) + index * 4096,
            'pid_read_bytes_scope': 'process_all_files', 'gpu_work_complete': True,
            'cache_state': 'full' if layers else 'omitted', 'stats': stats,
            'timing': {'enabled': True,
                       **{key: index * 100 for key in protocol.phase.TIMING_KEYS}},
            'layers': layers if layers else None})
    record = {'schema': protocol.phase.SCHEMA, 'request_id': 'chatcmpl-auto-1',
        'outcome': 'success', 'completeness': 'complete', 'input_tokens': 1024,
        'output_tokens': output, 'decode_forwards_completed': forwards,
        'snapshots': snapshots}
    response = {'response_id': record['request_id'], 'response_id_valid': True,
        'actual_input': 1024, 'actual_output': output, 'prompt_sha256': 'a' * 64,
        'text': 'fixed output', 'ttft': 1.1, 'latency': 3.,
        'finish': ['length'], 'timing': {'within_client_boundaries': True,
            'start_monotonic_seconds': 1., 'end_monotonic_seconds': 4.}}
    if enabled:
        refresh_serialization(record)
    return record, response


def refresh_serialization(record):
    size = sum(len(json.dumps(layer['supply_observer'], separators=(',', ':')).encode())
               for snapshot in record['snapshots'] if snapshot['layers'] is not None
               for layer in snapshot['layers'])
    record['supply_observer_serialization'] = {
        'bytes': size, 'limit_bytes': 1 << 20, 'complete': True}


def end_observer(record, layer=0):
    return record['snapshots'][-1]['layers'][layer]['supply_observer']


def service_fixture(count, output=256, enabled=False):
    record, response = fixture(output, enabled=enabled)
    records, responses = {}, []
    # This helper is used with no direct witnesses and no singleton prefill.
    for n in range(count):
        r, p = copy.deepcopy(record), copy.deepcopy(response)
        r['request_id'] = p['response_id'] = f'id-{n}'
        for snapshot in r['snapshots']:
            for key in protocol.phase.STAT_KEYS:
                snapshot['stats'][key] += n * record['snapshots'][-1]['stats'][key]
            if snapshot['layers'] is not None:
                for layer in snapshot['layers']:
                    for key in ('slot_clock', 'l2_clock'):
                        delta = (record['snapshots'][-1]['layers'][0][key] -
                                 record['snapshots'][0]['layers'][0][key])
                        layer[key] += n * delta
                    if enabled:
                        for key in protocol.COUNTERS:
                            layer['supply_observer']['counters'][key] += n * (
                                end_observer(record)['counters'][key])
        if enabled:
            refresh_serialization(r)
        records[r['request_id']] = r
        responses.append(p)
    return records, responses


class SequenceContracts(unittest.TestCase):
    def test_four_cells_only_new_observer_alternates(self):
        plans = [protocol.supply_plan(document(i), 'b' * 64) for i in range(1, 5)]
        self.assertEqual([p['group_id'] for p in plans],
            ['s01-as-off', 's02-as-on', 's03-al-on', 's04-al-off'])
        self.assertEqual([p['supply_observer_enabled'] for p in plans],
                         [False, True, True, False])
        self.assertEqual([p['requests'][0]['input_tokens'] for p in plans],
                         [1024, 1024, 8193, 8193])
        self.assertTrue(all(p['observation'] == 'on' and p['phase_diagnostics']
                            and not p['performance_acceptance'] for p in plans))
        self.assertEqual(plans[2]['requests'][1]['preceding_input_tokens'], 8193)
        identity = protocol.supply_request_identity(plans[0], plans[0]['requests'][0])
        self.assertIs(identity['supply_observer_enabled'], False)

    def test_sequence_rejects_scope_order_shape_and_bundle_changes(self):
        changes = [('arm', 'C'), ('index', True), ('observer', 0),
                   ('id', 'm02-as-on'), ('observation', 'off'),
                   ('pair_index', 2), ('predecessor_tokens', 8193)]
        for key, value in changes:
            doc = document()
            doc['group'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                protocol.supply_plan(doc, 'b' * 64)
        for mutation in ('extra', 'output', 'count', 'capacity', 'quota', 'sha'):
            doc = document()
            if mutation == 'extra':
                doc['extra'] = 1
            elif mutation == 'output':
                doc['group']['requests'][0]['output_tokens'] = 255
            elif mutation == 'count':
                doc['group']['requests'].append(doc['group']['requests'][0])
            elif mutation == 'quota':
                doc['trace_max_mib'] = 129
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                protocol.supply_plan(doc, 'invalid' if mutation == 'sha' else 'b' * 64,
                                     262143 if mutation == 'capacity' else 262144)

    def test_environment_removes_inherited_flags_and_preserves_only_frozen_axis(self):
        plan = protocol.supply_plan(document(), 'b' * 64)
        env = protocol.supply_environment(plan, {'PATH': '/bin',
            'Q4T_MOE_PARTITION': '1', 'Q4T_SURPRISE': '1', 'LD_PRELOAD': 'bad.so'})
        protocol.check_supply_environment(plan, env)
        self.assertEqual(env['PATH'], '/bin')
        self.assertEqual(env['Q4T_MOE_SUPPLY_OBSERVER'], '0')
        self.assertEqual(env['Q4T_OFFLOAD_PHASE_DIAGNOSTICS'], '1')
        self.assertNotIn('LD_PRELOAD', env)
        env['Q4T_MOE_SUPPLY_OBSERVER'] = '1'
        with self.assertRaises(ValueError):
            protocol.check_supply_environment(plan, env)
        with self.assertRaises(ValueError):
            protocol.observer_environment(1, {})

    def test_bound_sequence_reader_rejects_changed_bytes_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'sequence.json'
            raw = json.dumps(document()).encode()
            path.write_bytes(raw)
            plan, copy_bytes = protocol.read_supply_sequence(
                path, hashlib.sha256(raw).hexdigest())
            self.assertEqual(copy_bytes, raw)
            self.assertEqual(plan['group_id'], 's01-as-off')
            with self.assertRaises(ValueError):
                protocol.read_supply_sequence(path, '0' * 64)
            raw = b'{"schema":1,"schema":1}'
            path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                protocol.read_supply_sequence(path, hashlib.sha256(raw).hexdigest())


class SupplyContracts(unittest.TestCase):
    def test_complete_record_has_exact_true_decode_and_preserves_raw(self):
        record, response = fixture(direct=True)
        original = copy.deepcopy(record)
        result = protocol.audit_supply_record(record, response, True, True)
        self.assertEqual(record, original)
        self.assertIs(result['phase_record'], record)
        decode = result['supply_observer']['decode']
        self.assertEqual(len(decode['layers']), 48)
        self.assertEqual(decode['direct_read_losses'], 48 * 255)
        self.assertEqual(decode['layers'][0]['confirmed_phase_samples_not_retained'], 251)
        self.assertFalse(decode['prefix_layer_deltas_available'])
        self.assertLess(result['observer_additional_json_bytes'], 1 << 20)

    def test_quality_accepts_actual_variable_lengths_including_one(self):
        for output in (1, 7, 9, 33, 256):
            record, response = fixture(output)
            result = protocol.audit_supply_record(record, response, True)
            self.assertEqual(result['supply_observer']['decode']['counters']
                             ['plans_complete'], 48 * (output - 1))
        record, response = fixture(7)
        with self.assertRaisesRegex(ValueError, '256/length'):
            protocol.audit_supply_record(record, response, True, True)

    def test_singleton_prefill_does_not_apply_true_decode_entry_proof(self):
        record, response = fixture(prefill_single=True)
        result = protocol.audit_supply_record(record, response, True)
        prefill = result['supply_observer']['prefill']
        self.assertFalse(prefill['true_decode_entry_premise_applied'])
        self.assertEqual(prefill['counters']['source_mirror_outside_entry_candidates'], 48)
        self.assertEqual(result['supply_observer']['decode']['counters']
                         ['source_mirror_outside_entry_candidates'], 0)

    def test_off_requires_field_absence_and_on_requires_every_full_layer(self):
        record, response = fixture(enabled=False)
        result = protocol.audit_supply_record(record, response, False)
        self.assertEqual(result['observer_additional_json_bytes'], 0)
        self.assertNotIn('supply_observer', result)
        with self.assertRaisesRegex(ValueError, 'presence'):
            protocol.audit_supply_record(record, response, True)
        record, response = fixture()
        with self.assertRaisesRegex(ValueError, 'presence'):
            protocol.audit_supply_record(record, response, False)
        del record['snapshots'][1]['layers'][3]['supply_observer']
        with self.assertRaisesRegex(ValueError, 'presence'):
            protocol.audit_supply_record(record, response, True)

    def test_original_phase_identity_prefix_timing_and_cache_checks_still_apply(self):
        for mutation in ('id', 'prefix', 'partial', 'cache', 'timing'):
            record, response = fixture()
            if mutation == 'id':
                record['request_id'] = 'other'
            elif mutation == 'prefix':
                record['snapshots'].pop(2)
            elif mutation == 'partial':
                record['completeness'] = 'partial'
            elif mutation == 'cache':
                record['snapshots'][1]['layers'][0]['slot_experts'] = [0] * 256
            else:
                response['timing']['end_monotonic_seconds'] = 2.1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                protocol.audit_supply_record(record, response, True)

    def test_uint_schema_sizes_and_required_counter_fields(self):
        for key, value in (('schema', 'unknown'), ('enabled', 1),
                           ('plan_state_bytes', 4097), ('persistent_state_bytes', 0)):
            record, response = fixture()
            end_observer(record)[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                protocol.audit_supply_record(record, response, True)
        for value in (True, -1, 1 << 64, 0.5):
            record, response = fixture()
            end_observer(record)['counters']['source_read'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                protocol.audit_supply_record(record, response, True)
        record, response = fixture()
        del end_observer(record)['counters']['commit_errors']
        with self.assertRaises(ValueError):
            protocol.audit_supply_record(record, response, True)

    def test_unknown_loss_all_errors_and_unfinished_plans_rejected(self):
        for key in (*protocol.ERROR_KEYS, 'plans_started'):
            record, response = fixture()
            end_observer(record)['counters'][key] += 1
            with self.subTest(key=key), self.assertRaises(ValueError):
                protocol.audit_supply_record(record, response, True)

    def test_source_candidate_reservation_and_witness_count_closures(self):
        keys = ('planned_loads', 'source_read', 'committed_loads',
                'entry_mirror_candidate', 'entry_l2_present',
                'entry_candidate_to_mirror', 'writeback_pending_missing_targets',
                'writeback_reservations', 'samples_confirmed_total')
        for key in keys:
            record, response = fixture(direct=True)
            end_observer(record)['counters'][key] += 1
            with self.subTest(key=key), self.assertRaises(ValueError):
                protocol.audit_supply_record(record, response, True)

    def test_per_layer_clock_and_actual_decode_forward_counts(self):
        for key in ('l2_clock', 'slot_clock'):
            record, response = fixture()
            record['snapshots'][-1]['layers'][0][key] += 1
            with self.subTest(key=key), self.assertRaises(ValueError):
                protocol.audit_supply_record(record, response, True)
        record, response = fixture()
        # All aggregate counters can still close with a misplaced layer plan.
        end_observer(record, 0)['counters']['plans_complete'] += 1
        end_observer(record, 0)['counters']['plans_started'] += 1
        end_observer(record, 1)['counters']['plans_complete'] -= 1
        end_observer(record, 1)['counters']['plans_started'] -= 1
        with self.assertRaisesRegex(ValueError, 'per-layer plans'):
            protocol.audit_supply_record(record, response, True)

    def test_original_aggregate_source_and_single_shape_counters_close(self):
        for key in ('loads', 'l2_misses', 'mirror_hits', 'shape_single_misses',
                    'l2_shape_single_misses', 'resolve_calls',
                    'mirror_writebacks', 'mirror_skips'):
            record, response = fixture()
            record['snapshots'][-1]['stats'][key] += 1
            with self.subTest(key=key), self.assertRaises(ValueError):
                protocol.audit_supply_record(record, response, True)

    def test_decode_rejects_claimed_entry_even_when_prefill_allows_it(self):
        record, response = fixture()
        end_observer(record)['counters']['entry_claimed_mirrors'] = 1
        with self.assertRaisesRegex(ValueError, 'true decode entry'):
            protocol.audit_supply_record(record, response, True)

    def test_active_abort_and_published_cause_are_distinct_valid_witnesses(self):
        record, response = fixture(direct=True)
        obs = end_observer(record)
        obs['samples'][-1] = witness(0, 255, 512, final='aborted')
        obs['counters']['writeback_published'] -= 1
        obs['counters']['writeback_aborted'] += 1
        record['snapshots'][-1]['stats']['mirror_writebacks'] -= 1
        refresh_serialization(record)
        protocol.audit_supply_record(record, response, True)
        record, response = fixture(direct=True)
        obs = end_observer(record)
        obs['samples'][-1] = witness(0, 255, 512, state='published')
        obs['counters']['entry_candidate_to_read_direct_active'] -= 1
        obs['counters']['entry_candidate_to_read_direct_published'] += 1
        refresh_serialization(record)
        protocol.audit_supply_record(record, response, True)

    def test_causal_chronology_cross_task_and_success_are_mandatory(self):
        for key, value in (('reserve_seq', 3), ('publication_seq_or_zero', 2),
                ('source_task', 0), ('GPU_victim', 300), ('entry_L2_present', True),
                ('actual_source', 'MIRROR'), ('read_ok', False),
                ('commit_ok', False), ('plan_complete', False), ('expert', 512)):
            record, response = fixture(direct=True)
            end_observer(record)['samples'][-1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                protocol.audit_supply_record(record, response, True)

    def test_sample_retention_sequence_windows_and_phase_plan_window(self):
        for mutation in ('overwrite', 'sequence', 'length', 'phase'):
            record, response = fixture(direct=True)
            obs = end_observer(record)
            if mutation == 'overwrite':
                obs['counters']['samples_overwritten'] -= 1
            elif mutation == 'sequence':
                obs['samples'][0]['witness_seq'] -= 1
            elif mutation == 'length':
                obs['samples'].pop()
            else:
                obs['samples'][0]['plan_clock'] = 257
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                protocol.audit_supply_record(record, response, True)

    def test_additional_json_byte_cap_is_enforced_without_truncation(self):
        record, response = fixture()
        with mock.patch.object(protocol, 'MAX_ADDITIONAL_JSON_BYTES', 1):
            with self.assertRaisesRegex(ValueError, 'JSON byte cap'):
                protocol.audit_supply_record(record, response, True)

    def test_serialization_metadata_presence_count_cap_and_completeness(self):
        for key, value in (('bytes', 0), ('limit_bytes', (1 << 20) + 1),
                           ('complete', False)):
            record, response = fixture()
            record['supply_observer_serialization'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'serialization'):
                protocol.audit_supply_record(record, response, True)
        record, response = fixture()
        del record['supply_observer_serialization']
        with self.assertRaisesRegex(ValueError, 'serialization presence'):
            protocol.audit_supply_record(record, response, True)
        record, response = fixture(enabled=False)
        record['supply_observer_serialization'] = {
            'bytes': 0, 'limit_bytes': 1 << 20, 'complete': True}
        with self.assertRaisesRegex(ValueError, 'serialization presence'):
            protocol.audit_supply_record(record, response, False)

    def test_log_reader_preserves_uint_rejects_duplicate_fields_ids_and_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'server.log'
            record, _ = fixture()
            line = protocol.phase.PREFIX + json.dumps(record) + '\n'
            path.write_text('ordinary log\n' + line)
            sources = {}
            rows = protocol.read_supply_records(path, sources)
            self.assertEqual(rows[record['request_id']]['snapshots'][0]
                             ['pid_read_bytes'], 1 << 54)
            self.assertEqual(sources[str(path.resolve())],
                             hashlib.sha256(path.read_bytes()).hexdigest())
            for bad in (line + line, 'bad ' + line,
                        protocol.phase.PREFIX + '{"a":1,"a":2}\n'):
                path.write_text(bad)
                with self.assertRaises(ValueError):
                    protocol.read_supply_records(path, {})

    def test_group_counts_ids_and_diagnostic_conditioning_sequence(self):
        records, responses = service_fixture(4)
        result = protocol.audit_supply_group(records, responses, False,
                                              'diagnostic', [1024] * 4)
        self.assertEqual(len(result['requests']), 4)
        with self.assertRaisesRegex(ValueError, 'request count'):
            protocol.audit_supply_group(records, responses, False, 'quality')
        with self.assertRaisesRegex(ValueError, 'sequence'):
            protocol.audit_supply_group(records, responses, False,
                                         'diagnostic', [8193, 1024, 1024, 1024])
        responses[1]['response_id'] = responses[0]['response_id']
        with self.assertRaisesRegex(ValueError, 'IDs'):
            protocol.audit_supply_group(records, responses, False,
                                         'diagnostic', [1024] * 4)

    def test_quality_group_and_on_off_history_carry_are_checked(self):
        records, responses = service_fixture(11, output=1, enabled=True)
        result = protocol.audit_supply_group(records, responses, True, 'quality')
        self.assertEqual(len(result['requests']), 11)
        for enabled in (False, True):
            records, responses = service_fixture(4, enabled=enabled)
            protocol.audit_supply_group(records, responses, enabled,
                                         'diagnostic', [1024] * 4)
            # A valid within-request cache history can still begin from a reset.
            for snapshot in records['id-1']['snapshots']:
                if snapshot['layers'] is not None:
                    snapshot['layers'][0]['slot_ticks'][0] = 1
            with self.subTest(enabled=enabled), self.assertRaisesRegex(
                    ValueError, 'between requests'):
                protocol.audit_supply_group(records, responses, enabled,
                                             'diagnostic', [1024] * 4)


if __name__ == '__main__':
    unittest.main(verbosity=2)
