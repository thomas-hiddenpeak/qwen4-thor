"""Eighteen CPU-only contracts for the selected-request ledger analyzer."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ledger_analyze as m

PID, TID = 1234, 1235


def records(counts=(0, 512, 0, 0), cbids=None):
    cbids = cbids or [51] * len(counts)
    out = [dict(event='config', schema=1, mode='memset_ledger', pid=PID, tid=TID,
                runtime_cbids=[49, 51, 233, 235], record_capacity=m.MAX_RECORDS,
                thread_id_type='SYSTEM', collector_owned_bytes=16 * 1024 * 1024,
                configured_cupti_buffer_bytes=5 * 1024 * 1024,
                collector_limit_bytes=m.MAX_BYTES, log_limit_bytes=m.MAX_BYTES,
                host_buffer_count=8, host_buffer_bytes=1048576,
                device_buffer_count=4, device_buffer_bytes=1048576,
                libraries=[dict(name=name, path='/synthetic/' + name)
                           for name in ('cudart', 'cupti_linked', 'cublasLt')],
                runtime_version=13030, driver_version=13030, cupti_version=130303,
                cupti_header_api_version=28, cublasLt_version=130501),
           dict(event='boundary', site='start', pid=PID, tid=TID,
                original_return=0, timestamp=100)]
    sequence = 0
    activities = []
    for ordinal, (count, cbid) in enumerate(zip(counts, cbids)):
        sequence += 1
        corr = 10 + ordinal
        runtime = dict(event='api', seq=sequence, enter_seq=sequence,
                       domain='runtime', site='enter', name=m.RUNTIME_NAMES[cbid],
                       pid=PID, tid=TID, correlation_id=corr, phase='capture',
                       parent_runtime_correlation_id=0, parent_runtime_enter_seq=0,
                       context_uid=1, cbid=cbid, retval=-1, is_memset=True,
                       count=count, dev_ptr=4096 if ordinal < 2 else 8192,
                       value=90 if ordinal < 2 else 0, stream=0,
                       stream_parameter_present=cbid in (51, 235))
        out.append(runtime)
        if count:
            sequence += 1
            driver = dict(runtime, seq=sequence, enter_seq=sequence, domain='driver',
                          name='cuMemsetD8Async', cbid=216, correlation_id=corr + 1000,
                          parent_runtime_correlation_id=corr,
                          parent_runtime_enter_seq=runtime['enter_seq'],
                          is_memset=False, count=0, stream_parameter_present=False)
            out.append(driver)
            sequence += 1
            out.append(dict(driver, seq=sequence, site='exit', retval=0))
        sequence += 1
        out.append(dict(runtime, seq=sequence, site='exit', retval=0))
        start = 1000 + ordinal * 100
        activities.append(dict(event='activity', kind='runtime', name=runtime['name'],
                               correlation_id=corr, start=start, end=start + 10,
                               process_id=PID, process_id_source='activity_record', thread_id=TID,
                               cbid=cbid, return_value=0, context_id=0, device_id=0,
                               stream_id=0, runtime_correlation_id=None, bytes=0, value=0))
        if count:
            activities.append(dict(event='activity', kind='memset', name='',
                                   correlation_id=corr + 1000, start=start + 5, end=start + 20,
                                   process_id=PID, process_id_source='collector_process', thread_id=0,
                                   cbid=0, return_value=-1, context_id=1, device_id=0,
                                   stream_id=7, runtime_correlation_id=None, bytes=count,
                                   value=runtime['value'] & 255))
    for row in activities:
        sequence += 1
        out.append(dict(row, seq=sequence))
    out.append(dict(event='boundary', site='end', pid=PID, tid=TID,
                    original_return=0, timestamp=10000))
    out.append(dict(event='summary', mode='memset_ledger',
                    pid=PID, tid=TID, subscriber_used=True,
                    collector_limit_bytes=m.MAX_BYTES, collector_owned_bytes=16 * 1024 * 1024,
                    configured_cupti_buffer_bytes=5 * 1024 * 1024,
                    api_enter_count=sum(r.get('site') == 'enter' for r in out),
                    api_exit_count=sum(r.get('site') == 'exit' for r in out),
                    activity_count=len(activities), record_count=sequence,
                    errors=0, drops=0, overflow=0, buffers_outstanding=0,
                    buffer_requests=1, buffer_completions=1, flushed=True,
                    disabled=True, unsubscribed=True, completed=True, finalized=False,
                    cuda_sync_added=False, original_start_calls=1, original_stop_calls=1,
                    flush_flags=[0, 1]))
    return out


def resequence(rows):
    mapping, sequence = {}, 0
    for row in rows:
        if row['event'] in ('api', 'activity'):
            sequence += 1
            if row.get('site') == 'enter':
                mapping[row['enter_seq']] = sequence
            row['seq'] = sequence
    for row in rows:
        if row['event'] == 'api':
            row['enter_seq'] = mapping.get(row['enter_seq'], row['enter_seq'])
            row['parent_runtime_enter_seq'] = mapping.get(row['parent_runtime_enter_seq'],
                                                         row['parent_runtime_enter_seq'])
    rows[-1].update(api_enter_count=sum(r.get('site') == 'enter' for r in rows),
                    api_exit_count=sum(r.get('site') == 'exit' for r in rows),
                    activity_count=sum(r['event'] == 'activity' for r in rows), record_count=sequence)
    return rows


def probe_log(path):
    # Reuse only the old synthetic fixture builder, never its tests or parser.
    from test_mechanism_analyze import records as probe_records, write_log
    rows = probe_records('nsys')
    for _, row in rows:
        if 'pid' in row:
            row['pid'] = PID
        if 'tid' in row:
            row['tid'] = TID
    write_log(path, rows)


class LedgerAnalysisContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.jsonl'

    def run_records(self, rows, **kwargs):
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        return m.analyze(self.path, **kwargs)

    def assert_invalid(self, rows, **kwargs):
        result = self.run_records(rows, **kwargs)
        self.assertEqual(result['status'], 'INVALID', result)
        self.assertTrue(result['errors'])
        return result

    def test_four_cbids_actual_parameters_and_system_tid(self):
        result = self.run_records(records(cbids=[49, 51, 233, 235]), expected_pid=PID)
        self.assertEqual(result['status'], 'VALID', result)
        self.assertEqual(result['counts'], dict(runtime_memset_calls=4, driver_bridge_calls=1,
                                               zero_calls=3, nonzero_calls=1,
                                               memset_activities=1, nonzero_bytes=512))
        self.assertFalse(result['performance_acceptance'])

    def test_json_truncation_duplicate_keys_and_bound(self):
        for raw in ('{}', '{"event":"config","event":"summary"}\n', '[]\n', '\n'):
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                self.assertEqual(m.analyze(self.path)['status'], 'INVALID')
        with patch.object(m, 'MAX_BYTES', 32):
            self.assert_invalid(records())

    def test_pid_and_system_tid_are_not_interchangeable(self):
        self.assert_invalid(records(), expected_pid=PID + 1)
        for event, field in (('api', 'pid'), ('activity', 'process_id'), ('activity', 'thread_id')):
            rows = records()
            row = next(r for r in rows if r['event'] == event)
            row[field] += 1
            with self.subTest(field=field):
                self.assert_invalid(rows)

    def test_sequence_duplicate_and_gap(self):
        for change in (-1, 1):
            rows = records()
            next(r for r in rows if r['event'] == 'activity')['seq'] += change
            with self.subTest(change=change):
                self.assert_invalid(rows)

    def test_api_pair_parameters_return_and_missing_exit(self):
        for field, value in (('count', 999), ('retval', 1), ('tid', TID + 1)):
            rows = records()
            next(r for r in rows if r.get('site') == 'exit')[field] = value
            with self.subTest(field=field):
                self.assert_invalid(rows)
        rows = records()
        rows.remove(next(r for r in rows if r.get('site') == 'exit'))
        self.assert_invalid(resequence(rows))

    def test_driver_requires_exact_runtime_parent(self):
        for field, value in (('parent_runtime_correlation_id', 0), ('parent_runtime_enter_seq', 999),
                             ('tid', TID + 1), ('cbid', 999), ('name', 'cuMemsetUnknown')):
            rows = records()
            for row in rows:
                if row.get('domain') == 'driver':
                    row[field] = value
            with self.subTest(field=field):
                self.assert_invalid(rows)

    def test_sequential_driver_aliases_have_one_owner(self):
        rows = records()
        index = next(i for i, r in enumerate(rows) if r.get('domain') == 'driver' and r['site'] == 'exit')
        first = rows[index - 1]
        second = dict(first, enter_seq=999, seq=999)
        rows[index + 1:index + 1] = [second, dict(second, site='exit', retval=0)]
        result = self.run_records(resequence(rows))
        self.assertEqual(result['status'], 'VALID', result)
        self.assertEqual(result['counts']['driver_bridge_calls'], 2)
        self.assertEqual(result['counts']['nonzero_calls'], 1)

    def test_runtime_activity_missing_or_wrong_identity(self):
        for field, value in (('cbid', 49), ('name', 'cudaMemset'), ('return_value', 7)):
            rows = records()
            next(r for r in rows if r.get('kind') == 'runtime')[field] = value
            with self.subTest(field=field):
                self.assert_invalid(rows)
        rows = records()
        rows.remove(next(r for r in rows if r.get('kind') == 'runtime'))
        self.assert_invalid(resequence(rows))

    def test_nonzero_activity_missing_bytes_or_value(self):
        for field, value in (('bytes', 0), ('bytes', 511), ('value', 0)):
            rows = records()
            next(r for r in rows if r.get('kind') == 'memset')[field] = value
            with self.subTest(field=field, value=value):
                self.assert_invalid(rows)
        rows = [r for r in records() if r.get('kind') != 'memset']
        self.assert_invalid(resequence(rows))

    def test_zero_arguments_cannot_hide_nonzero_activity(self):
        rows = records()
        for row in rows:
            if row.get('domain') == 'runtime' and row.get('count') == 512:
                row['count'] = 0
        self.assert_invalid(rows)
        result = self.run_records(records((0, 0)))
        self.assertEqual(result['status'], 'VALID', result)
        self.assertEqual(result['counts']['nonzero_calls'], 0)

    def test_unknown_kind_incomplete_and_reversed_timestamps(self):
        for field, value in (('kind', 'kernel'), ('start', 0), ('end', 1),
                             ('process_id_source', 'guessed'), ('runtime_correlation_id', 11)):
            rows = records()
            next(r for r in rows if r.get('kind') == 'memset')[field] = value
            with self.subTest(field=field):
                self.assert_invalid(rows)

    def test_memset_duplicate_and_unknown_bridge(self):
        rows = records()
        gpu = next(r for r in rows if r.get('kind') == 'memset')
        rows.insert(-2, copy.deepcopy(gpu))
        self.assert_invalid(resequence(rows))
        rows = records()
        next(r for r in rows if r.get('kind') == 'memset')['correlation_id'] = 10
        self.assert_invalid(rows)

    def test_summary_loss_lifecycle_and_counts(self):
        for field, value in (('drops', 1), ('overflow', 1), ('errors', 1),
                             ('buffers_outstanding', 1), ('record_count', 2),
                             ('flushed', False), ('disabled', False), ('unsubscribed', False),
                             ('completed', False), ('finalized', True), ('cuda_sync_added', True)):
            rows = records()
            rows[-1][field] = value
            with self.subTest(field=field):
                self.assert_invalid(rows)

    def test_capture_boundary_and_frozen_configuration(self):
        for field, value in (('thread_id_type', 'DEFAULT'), ('record_capacity', 2),
                             ('collector_owned_bytes', m.MAX_BYTES), ('runtime_cbids', [51])):
            rows = records()
            rows[0][field] = value
            with self.subTest(field=field):
                self.assert_invalid(rows)
        for field, value in (('original_start_calls', 2), ('original_stop_calls', 0), ('flush_flags', [1])):
            rows = records()
            rows[-1][field] = value
            with self.subTest(field=field):
                self.assert_invalid(rows)

    def test_fixture_exact_oracle_and_distinct_mode_label(self):
        ledger = self.run_records(records())
        probe = Path(self.temp.name) / 'probe.log'
        probe_log(probe)
        result = m.validate_fixture(ledger, probe, PID)
        self.assertEqual(result['status'], 'VALID', result)
        self.assertEqual(result['exact_output_checks'], 4)
        self.assertFalse(result['nsight_was_run'])
        probe.write_text(probe.read_text().replace('"mismatches": 0', '"mismatches": 1', 1))
        self.assertEqual(m.validate_fixture(ledger, probe)['status'], 'INVALID')

    def test_fixture_unknown_call_or_wrong_order_blocks_model(self):
        probe = Path(self.temp.name) / 'probe.log'
        probe_log(probe)
        for counts in ((512, 0, 0, 0), (0, 512, 0), (0, 512, 0, 0, 0)):
            result = m.validate_fixture(self.run_records(records(counts)), probe)
            with self.subTest(counts=counts):
                self.assertEqual(result['status'], 'INVALID', result)
                self.assertFalse(result['eligible_model'])

    def test_model_native_counts_and_request_closure(self):
        path = Path(self.temp.name) / 'server.log'
        lines = [f'[exec-capture] event=begin pid={PID} tid={TID} input_tokens=8170 error=0']
        lines += [f'[exec-span] pid={PID} tid={TID} batch={n} id=19 parent=0 '
                  'phase=mtp.verify_forward seq=0 p=8170 t=4 closed=1' for n in range(74)]
        lines += ['[mtp-accept] request=test draft_counts=7,8,7,52',
                  f'[exec-capture] event=end pid={PID} tid={TID} input_tokens=8170 error=0',
                  '[q4t][request] instance=0 id=test event=end stage=response seq=0 '
                  'released=1 outcome=success reason=completed scheduler_waits=77']
        path.write_text('\n'.join(lines) + '\n')
        ledger = self.run_records(records())
        result = m.validate_model(ledger, path, PID)
        self.assertEqual(result['status'], 'VALID', result)
        self.assertEqual(result['oracle_counts']['t1'], 3)
        path.write_text(path.read_text().replace('scheduler_waits=77', 'scheduler_waits=76'))
        self.assertEqual(m.validate_model(ledger, path)['status'], 'INVALID')

    def test_symlink_and_fifo_rejected_without_read_block(self):
        self.run_records(records())
        link = Path(self.temp.name) / 'link'
        link.symlink_to(self.path)
        self.assertEqual(m.analyze(link)['status'], 'INVALID')
        import os
        fifo = Path(self.temp.name) / 'fifo'
        os.mkfifo(fifo)
        self.assertEqual(m.analyze(fifo)['status'], 'INVALID')


if __name__ == '__main__':
    unittest.main()
