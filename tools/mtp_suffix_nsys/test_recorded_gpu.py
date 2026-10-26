"""Sixteen offline contracts; no capture, model, HTTP, export, or sensors."""
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import recorded_gpu as m
import nvlog_snapshot
from test_analyze_gpu import make_database, cpu_log, PID, TID, GPID


def terminal_log(target=PID, source_pid=None, *, markers=True, filters=True):
    source_pid = target if source_pid is None else source_pid
    rows = [
        f'I01:00:00.000| Injection | target pid={target} tid= {target} | Init.cpp:1 [X]: Sending service event: CUDAFinish.',
        f'I01:00:00.000| Injection | target pid={target} tid= {target} | Init.cpp:2 [X]: ReportCUDADone: DONE.',
        f'I01:00:00.000| Injection | target pid={target} tid= {target} | Init.cpp:3 [X]: Sending service event: NVTXFinish.',
        'I01:00:00.001| consumer | nsys pid=700 tid= 701 | Trace.cpp:1 [Stop]: Trace[0xabcd]: Stop(1000)',
    ]
    if markers:
        for index, name in enumerate(('CUDA profiling finished', 'NVTX profiling finished')):
            rows.append('I01:00:00.002| consumer | nsys pid=700 tid= 702 | Trace.cpp:2 '
                        '[ReadStorageImpl]: { Timestamp=2000 Event={ Trace={ Name={ Text="' + name + '" } } } }')
            if filters:
                rows.append('I01:00:00.002| consumer | nsys pid=700 tid= 702 | Trace.cpp:3 '
                            '[ConvertTimeAndSerializeEvent]: Trace[0xabcd]: Event ignored: start timestamp '
                            f'{1100 + index} > session stop timestamp 1000')
    rows.append('I01:00:00.003| consumer | nsys pid=700 tid= 702 | Trace.cpp:4 '
                f'[ReadStorageImpl]: Trace[0xabcd]: Events diagnostic: 2, service: 0, added: 3, ignored: 2, from file pid {source_pid}, uid 9.')
    return '\n'.join(rows) + '\n'


class RecordedGpuContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.number = 0

    def capture(self, mutation=None, log=None, warnings=True, missing=True):
        self.number += 1
        output = self.base / str(self.number)
        output.mkdir()
        database = output/'trace.sqlite'
        make_database(database)
        with sqlite3.connect(database) as db:
            db.execute('DELETE FROM NVTX_EVENTS')
            db.executemany('INSERT INTO NVTX_EVENTS VALUES(?,?,?,?,?,?,?)', [
                (10,600,TID,59,'q4t|1|0|target_prefill_call|seq=0|p=0|t=8',None,None),
                (1000,1600,TID,59,'q4t|2|0|mtp.step|seq=0|p=8|t=4',None,None)])
            if missing:
                db.execute('DELETE FROM CUPTI_ACTIVITY_KIND_MEMSET')
            if warnings:
                db.executemany('INSERT INTO DIAGNOSTIC_EVENT VALUES(?,?,?)',
                               [(2,text,GPID) for text in sorted(m.TARGET_WARNINGS)])
            if mutation:
                mutation(db)
        (output/'server.log').write_text(cpu_log())
        (output/'export.log').write_text('Export completed.\n')
        nvlog = output/'nvlog.log'
        nvlog.write_text(terminal_log() if log is None else log)
        receipt = nvlog_snapshot.snapshot(nvlog, output/'nvlog.snapshot')
        (output/'snapshot.json').write_text(json.dumps(receipt))
        return output

    def analyze(self, output, **kwargs):
        return m.analyze_capture(output, kwargs.pop('target_pid', PID), kwargs.pop('wrapper_pid', None),
            scratch_dir=output/'scratch', nvlog_path=output/'nvlog.log',
            nvlog_snapshot_receipt=output/'snapshot.json', **kwargs)

    def test_01_strict_preserved_and_unknown_never_filled_zero(self):
        output = self.capture()
        before = m.strict.sha(output/'trace.sqlite')
        with patch.object(m.strict, 'audit_database', wraps=m.strict.audit_database) as audit:
            result = self.analyze(output)
        self.assertEqual(audit.call_count, 1)
        self.assertTrue(result['qualification_passed'], result)
        saved = json.loads((output/'strict-audit.json').read_text())
        self.assertEqual(saved['trace_integrity'], 'FAIL')
        self.assertEqual(len(saved['diagnostics']['warnings_require_review']), 2)
        missing = result['all_unmatched_memory']
        self.assertEqual(len(missing), 1)
        self.assertIsNone(missing[0]['observed_count_argument'])
        self.assertFalse(missing[0]['zero_inferred'])
        self.assertFalse(result['performance_acceptance'])
        self.assertEqual(before, m.strict.sha(output/'trace.sqlite'))

    def test_02_deleted_kernel_rejected_even_with_terminal_pair(self):
        output = self.capture(lambda db: db.execute('DELETE FROM CUPTI_ACTIVITY_KIND_KERNEL'))
        self.assertFalse(self.analyze(output)['qualification_passed'])

    def test_03_missing_copy_is_not_zero_memset(self):
        output = self.capture(lambda db: db.execute('DELETE FROM CUPTI_ACTIVITY_KIND_MEMCPY WHERE correlationId=1'))
        result = self.analyze(output)
        self.assertFalse(result['qualification_passed'])
        self.assertTrue(any('missing copy' in error for error in result['errors']), result)

    def test_04_new_duplicate_foreign_and_single_warning_rejected(self):
        mutations = [
            lambda db: db.execute('INSERT INTO DIAGNOSTIC_EVENT VALUES(?,?,?)', (2,'new warning',GPID)),
            lambda db: db.execute('INSERT INTO DIAGNOSTIC_EVENT VALUES(?,?,?)', (2,sorted(m.TARGET_WARNINGS)[0],GPID)),
            lambda db: db.execute('UPDATE DIAGNOSTIC_EVENT SET globalPid=?', ((PID+1)<<24,)),
            lambda db: db.execute('DELETE FROM DIAGNOSTIC_EVENT WHERE text=?', (sorted(m.TARGET_WARNINGS)[0],)),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.assertFalse(self.analyze(self.capture(mutate))['qualification_passed'])

    def test_05_wrong_producer_and_consumer_file_pid_rejected(self):
        for log in (terminal_log(target=PID+1), terminal_log(source_pid=PID+1)):
            with self.subTest(log=log):
                self.assertFalse(self.analyze(self.capture(log=log))['qualification_passed'])

    def test_06_filter_binds_own_event_and_session_stop(self):
        logs = [terminal_log().replace('Stop(1000)', 'Stop(999)'),
                terminal_log().replace('timestamp 1100', 'timestamp 900'),
                terminal_log().replace('Trace[0xabcd]: Event ignored', 'Trace[0xffff]: Event ignored'),
                terminal_log(filters=False)]
        logs.append(terminal_log() + 'I01:00:01.000| consumer | nsys pid=700 tid= 703 | '
                    'Trace.cpp:2 [ReadStorageImpl]: { Text="CUDA profiling finished" }\n')
        for index, log in enumerate(logs):
            with self.subTest(index=index):
                self.assertFalse(self.analyze(self.capture(log=log))['qualification_passed'])

    def test_07_all_missing_rows_not_first_32_examples(self):
        def mutate(db):
            for index in range(40):
                db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES(?,?,?,?,?,?,?)',
                           (220+index,221+index,TID,100+index,3 if index<39 else 1,0,None))
        result = self.analyze(self.capture(mutate))
        self.assertFalse(result['qualification_passed'])
        self.assertEqual(len(result['all_unmatched_memory']), 41)
        self.assertEqual(sum(r['name'].startswith('cudaMemcpy') for r in result['all_unmatched_memory']), 1)

    def test_08_other_missing_memset_name_rejected(self):
        output = self.capture(lambda db: db.execute('UPDATE StringIds SET value=? WHERE id=3', ('cudaMemset_v3020',)))
        self.assertFalse(self.analyze(output)['qualification_passed'])

    def test_09_no_warning_does_not_invent_required_latefilter(self):
        for log in (terminal_log(markers=False), terminal_log(filters=False)):
            with self.subTest(log=log):
                output = self.capture(warnings=False, missing=False, log=log)
                result = self.analyze(output)
                self.assertTrue(result['qualification_passed'], result)
                self.assertEqual(result['strict_audit']['trace_integrity'], 'PASS')
                self.assertFalse(result['terminal_evidence']['late_filter_pair_observed'])

    def test_10_explicit_loss_and_actual_target_error_rejected(self):
        for extra in ('I01:00:01.000| CUPTI | target pid=42 tid= 42 | X:1 [X]: CUPTI dropped 1 events.\n',
                      'E01:00:01.000| Injection | target pid=42 tid= 42 | X:1 [X]: Failure.\n'):
            with self.subTest(extra=extra):
                self.assertFalse(self.analyze(self.capture(log=terminal_log()+extra))['qualification_passed'])
        known = '\n'.join([
            'E00:59:59.000| Cupti_Public | target pid=42 tid= 42 | :6615 []: Error: Invalid parameter for enabling raw timestamp: 255.',
            'I00:59:59.001| quadd_cuda_tracing | target pid=42 tid= 42 | CallbackHandler.cpp:1387 [ResourceCallbackHandler]: Enabling HES trace via CUPTI',
            'E00:59:59.002| Cupti_Public | target pid=42 tid= 42 | :7484 []: CUPTI Error 27: CUPTI_ERROR_NOT_SUPPORTED',
            'E00:59:59.002| Cupti_Public | target pid=42 tid= 42 | :7484 []: Failed to enable HW trace.',
            'I00:59:59.003| quadd_cuda_tracing | target pid=42 tid= 42 | CallbackHandler.cpp:1412 [ResourceCallbackHandler]: ' + m.FALLBACK,
            'E00:59:59.004| Injection | target pid=42 tid= 42 | CudaInjectionInit.cpp:1897 [CheckForHesUsage]: HES data collection failed, CUPTI_ERROR_NOT_SUPPORTED',
            'E00:59:59.005| Injection | target pid=42 tid= 43 | CudaInjectionInit.cpp:1897 [CheckForHesUsage]: HES data collection failed, CUPTI_ERROR_NOT_SUPPORTED',
        ]) + '\n'
        passed = self.analyze(self.capture(log=known+terminal_log()))
        self.assertTrue(passed['qualification_passed'], passed)
        self.assertEqual(len(passed['terminal_evidence']['backend']['classified_error_records']), 5)
        for log in (known.replace(m.FALLBACK, 'missing fallback'),
                    known.replace('pid=42 tid= 42 | CallbackHandler.cpp:1412',
                                  'pid=43 tid= 42 | CallbackHandler.cpp:1412'),
                    known.replace('CUPTI Error 27:', 'CUPTI Error 99:')):
            with self.subTest(log=log):
                self.assertFalse(self.analyze(self.capture(log=log+terminal_log()))['qualification_passed'])
        auxiliary = ''.join('E01:00:01.000| Cupti_Public | target pid=42 tid= 42 | :1918 []: '
            f'Error: Activity kind {kind} cannot be disabled at context level.\n'
            for kind in (36, 35, 34, 33))
        report = ('I01:00:02.000| qdstrm_import | nsys pid=700 tid= 701 | '
                  'QdstrmImporter.cpp:591 [SaveReport]: Report file saved: /owned/trace.1.nsys-rep\n')
        def storage(allocated=35, returned=35):
            return ('E01:00:03.000| Injection | target pid=42 tid= 42 | MemoryStorage.cpp:65 '
                    f'[~MemoryStorage]: MemoryStorage 0xabcd was not deleted before the destructor. '
                    f'AllocatedCount: {allocated}, ReturnedCount: {returned}\n')
        six = terminal_log() + auxiliary + report + storage() + storage(1020242, 1020242)
        result = self.analyze(self.capture(log=six))
        self.assertFalse(result['qualification_passed'])
        proof = result['terminal_evidence']
        self.assertEqual(len(proof['backend']['unknown_error_records']), 6)
        classified = proof['forensic_error_classifications']
        self.assertEqual([r['activity_kind'] for r in classified[:4]], [36, 35, 34, 33])
        self.assertTrue(all(r['classification'] == 'POST_REPORT_DESTRUCTOR_BALANCED_COUNTERS'
                            for r in classified[4:]))
        self.assertTrue(all(r['resolution'] == 'UNRESOLVED' and not r['qualification_allowed']
                            for r in classified))
        self.assertTrue(all(not r['loss_free_proven'] and not r['report_identity_verified']
                            for r in classified[4:]))
        for label, text, expected in [
            ('before report', storage()+report, 'MEMORY_STORAGE_DESTRUCTOR_UNRESOLVED'),
            ('no report', storage(), 'MEMORY_STORAGE_DESTRUCTOR_UNRESOLVED'),
            ('unequal', report+storage(35, 34), 'MEMORY_STORAGE_DESTRUCTOR_UNRESOLVED'),
            ('ambiguous report', report+report+storage(), 'MEMORY_STORAGE_DESTRUCTOR_UNRESOLVED'),
            ('other kind', auxiliary.replace('kind 36', 'kind 37'), 'UNKNOWN_TARGET_ERROR'),
            ('fatal', auxiliary.replace('E01:', 'F01:'), 'UNKNOWN_TARGET_ERROR')]:
            with self.subTest(error_case=label):
                evidence = m.terminal_evidence((terminal_log()+text).encode(), PID)
                self.assertEqual(evidence['forensic_error_classifications'][0]['classification'], expected)
                self.assertTrue(evidence['qualification_errors'])

    def test_11_recorded_nvtx_and_correlation_failures_rejected(self):
        changes = [lambda db: db.execute('UPDATE NVTX_EVENTS SET end=NULL'),
                   lambda db: db.execute('UPDATE CUPTI_ACTIVITY_KIND_KERNEL SET globalPid=?', ((PID+1)<<24,)),
                   lambda db: db.execute('UPDATE CUPTI_ACTIVITY_KIND_KERNEL SET correlationId=999')]
        for index, mutate in enumerate(changes):
            with self.subTest(index=index):
                self.assertFalse(self.analyze(self.capture(mutate))['qualification_passed'])

    def test_12_snapshot_prefix_change_rejected(self):
        output = self.capture()
        original = (output/'nvlog.log').read_text()
        (output/'nvlog.log').write_text(original.replace('ReportCUDADone', 'ReportXUDADone'))
        self.assertFalse(self.analyze(output)['qualification_passed'])

    def test_13_output_overwrite_refused_before_second_audit(self):
        output = self.capture()
        first = self.analyze(output)
        before = m.strict.sha(output/'recorded-gpu-audit.json')
        self.assertTrue(first['qualification_passed'], first)
        with patch.object(m.strict, 'audit_database') as audit:
            with self.assertRaises(ValueError):
                self.analyze(output)
        audit.assert_not_called()
        self.assertEqual(before, m.strict.sha(output/'recorded-gpu-audit.json'))

    def test_14_fixed_source_and_time_bounds_fail_closed(self):
        output = self.capture()
        with patch.object(m, 'MAX_SQLITE', 1):
            self.assertFalse(self.analyze(output)['qualification_passed'])
        output = self.capture()
        with patch.object(m, 'MAX_SECONDS', -1):
            self.assertFalse(self.analyze(output)['qualification_passed'])
        data = terminal_log().encode()
        class OnePassLines:
            def __init__(self):
                self.used = False
            def __iter__(self):
                if self.used:
                    raise AssertionError('terminal parser reread the source')
                self.used = True
                yield from io.BytesIO(data)
            def read(self, *args):
                raise AssertionError('terminal parser attempted a whole-source read')
        streamed = m.terminal_evidence_lines(OnePassLines(), PID)
        self.assertEqual(streamed, m.terminal_evidence(data, PID))
        self.assertEqual(streamed['raw_source']['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(streamed['raw_source']['bytes'], len(data))
        first = streamed['producer_finish']['CUDAFinish'][0]
        self.assertEqual(first['byte_offset'], 0)
        self.assertEqual(first['raw_sha256'], hashlib.sha256(next(iter(io.BytesIO(data)))).hexdigest())
        second_stop = b'I01:00:00.001| consumer | nsys pid=700 tid= 701 | Trace.cpp:1 [Stop]: Trace[0xffff]: Stop(1001)\n'
        marker = b'I01:00:00.002| consumer | nsys pid=700 tid= 702 | Trace.cpp:2 [ReadStorageImpl]: { Text="CUDA profiling finished" }\n'
        cases = [
            ('line', 'MAX_LINE', 8, data),
            ('logical bytes', 'MAX_STREAM_LOG', 20, data),
            ('evidence count', 'MAX_TERMINAL_EVIDENCE', 2, data),
            ('evidence bytes', 'MAX_TERMINAL_EVIDENCE_BYTES', 32, data),
            ('sessions', 'MAX_TERMINAL_SESSIONS', 1, data+second_stop),
            ('threads', 'MAX_TERMINAL_THREADS', 1, marker+marker.replace(b'tid= 702', b'tid= 703'))]
        for label, attribute, value, content in cases:
            with self.subTest(bound=label), patch.object(m, attribute, value):
                with self.assertRaises(ValueError):
                    m.terminal_evidence_lines(io.BytesIO(content), PID)
        for bad in ([data], ['text'], [b''], [b'partial', b'next\n'], [b'\xff\n']):
            with self.subTest(iterator=repr(bad)[:100]):
                with self.assertRaises((ValueError, UnicodeError)):
                    m.terminal_evidence_lines(iter(bad), PID)
        partial = m.terminal_evidence_lines(io.BytesIO(data.rstrip(b'\n')), PID)
        self.assertFalse(partial['raw_source']['all_lines_terminated'])
        self.assertTrue(partial['qualification_errors'])

    def test_15_new_api_error_and_full_scan_count_mismatch_rejected(self):
        output = self.capture(lambda db: db.execute('UPDATE CUPTI_ACTIVITY_KIND_RUNTIME SET returnValue=1 WHERE correlationId=2'))
        self.assertFalse(self.analyze(output)['qualification_passed'])
        output = self.capture()
        with patch.object(m, 'all_missing_memory', return_value=[]):
            self.assertFalse(self.analyze(output)['qualification_passed'])

    def test_16_foreign_process_rows_do_not_supply_missing_target_work(self):
        def mutate(db):
            db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_MEMSET VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (180,210,(PID+1)<<24,4,0,1,8,16,1,0))
        result = self.analyze(self.capture(mutate))
        self.assertTrue(result['qualification_passed'], result)
        self.assertEqual(len(result['all_unmatched_memory']), 1)


if __name__ == '__main__':
    unittest.main()
