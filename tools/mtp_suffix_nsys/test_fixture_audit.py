"""Synthetic SQLite contracts; no CUDA, profiler, HTTP or sensor dependency."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import fixture_audit as subject

PID = 1234
TID = 1234
GLOBAL_PID = PID << 24
GLOBAL_TID = GLOBAL_PID | TID


def make_fixture(directory):
    database, receipt = directory / 'trace.sqlite', directory / 'server.log'
    with sqlite3.connect(database) as db:
        db.executescript('''
            CREATE TABLE StringIds(id INTEGER PRIMARY KEY, value TEXT);
            CREATE TABLE CUPTI_ACTIVITY_KIND_RUNTIME(start INTEGER,end INTEGER,
                globalTid INTEGER,correlationId INTEGER,nameId INTEGER,returnValue INTEGER);
            CREATE TABLE CUPTI_ACTIVITY_KIND_KERNEL(start INTEGER,end INTEGER,
                globalPid INTEGER,correlationId INTEGER,demangledName INTEGER,
                deviceId INTEGER,contextId INTEGER,streamId INTEGER,
                gridX INTEGER,gridY INTEGER,gridZ INTEGER,
                blockX INTEGER,blockY INTEGER,blockZ INTEGER);
            CREATE TABLE CUPTI_ACTIVITY_KIND_MEMCPY(start INTEGER,end INTEGER,
                globalPid INTEGER,correlationId INTEGER,copyKind INTEGER,bytes INTEGER,
                deviceId INTEGER,contextId INTEGER,streamId INTEGER,copyCount INTEGER);
            CREATE TABLE CUPTI_ACTIVITY_KIND_MEMSET(start INTEGER,end INTEGER,
                globalPid INTEGER,correlationId INTEGER,value INTEGER,bytes INTEGER,
                deviceId INTEGER,contextId INTEGER,streamId INTEGER);
            CREATE TABLE NVTX_EVENTS(start INTEGER,end INTEGER,text TEXT,
                globalTid INTEGER,endGlobalTid INTEGER,textId INTEGER);
            CREATE TABLE ENUM_CUDA_MEMCPY_OPER(id INTEGER,name TEXT);
            CREATE TABLE ENUM_DIAGNOSTIC_SEVERITY_LEVEL(id INTEGER,name TEXT);
            CREATE TABLE DIAGNOSTIC_EVENT(timestamp INTEGER,timestampType INTEGER,
                source INTEGER,severity INTEGER,text TEXT,globalPid INTEGER);
        ''')
        db.executemany('INSERT INTO StringIds VALUES(?,?)', [
            (1, 'cudaLaunchKernel_v7000'), (2, 'cudaMemcpyAsync_v3020'),
            (3, 'cudaStreamSynchronize_v3020'), (4, 'void K3FixtureKernel(unsigned int*, int)'),
            (5, 'cudaMemsetAsync_v3020')])
        db.executemany('INSERT INTO ENUM_CUDA_MEMCPY_OPER VALUES(?,?)',
                       [(1, 'CUDA_MEMCPY_KIND_HTOD'), (2, 'CUDA_MEMCPY_KIND_DTOH')])
        db.executemany('INSERT INTO ENUM_DIAGNOSTIC_SEVERITY_LEVEL VALUES(?,?)',
                       [(0, 'UnknownLevel'), (1, 'Info'), (2, 'Warning'), (3, 'Error')])
        db.execute('INSERT INTO NVTX_EVENTS VALUES(?,?,?,?,?,?)',
                   (0, 33000, 'k3fixture|root', GLOBAL_TID, None, None))
        for i in range(32):
            start, correlation = 100 + i * 1000, 1000 + i * 4
            db.execute('INSERT INTO NVTX_EVENTS VALUES(?,?,?,?,?,?)',
                       (start, start + 400, 'k3fixture|step=' + str(i), GLOBAL_TID, None, None))
            db.executemany('INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES(?,?,?,?,?,?)', [
                (start + 10, start + 20, GLOBAL_TID, correlation, 2, 0),
                (start + 25, start + 28, GLOBAL_TID, correlation + 1, 5, 0),
                (start + 30, start + 40, GLOBAL_TID, correlation + 2, 1, 0),
                (start + 50, start + 60, GLOBAL_TID, correlation + 3, 2, 0)])
            db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_KERNEL VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (start + 160, start + 200, GLOBAL_PID, correlation + 2, 4,
                 0, 1, 7, 16, 1, 1, 256, 1, 1))
            db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_MEMSET VALUES(?,?,?,?,?,?,?,?,?)',
                       (start + 151, start + 155, GLOBAL_PID, correlation + 1, 0,
                        512 if i % 2 == 0 else 16384, 0, 1, 7))
            db.executemany('INSERT INTO CUPTI_ACTIVITY_KIND_MEMCPY VALUES(?,?,?,?,?,?,?,?,?,?)', [
                (start + 100, start + 150, GLOBAL_PID, correlation, 1, 16384, 0, 1, 7, 1),
                (start + 210, start + 250, GLOBAL_PID, correlation + 3, 2, 16384, 0, 1, 7, 1)])
        db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES(?,?,?,?,?,?)',
                   (32500, 32900, GLOBAL_TID, 9999, 3, 0))
    config = dict(schema=1, pid=PID, tid=TID, iterations=32, elements=4096,
                  bytes_per_copy=16384, expected_kernels=32, expected_h2d=32,
                  expected_d2h=32, expected_memset_apis=32,
                  expected_nvtx=33, release_timeout_seconds=60)
    lines = ['[fixture-config] ' + json.dumps(config)]
    for i in range(32):
        input_sum = 4096 * 4095 // 2 + 4096 * 4096 * i
        memset_bytes = 512 if i % 2 == 0 else 16384
        cleared = memset_bytes // 4
        cleared_input = cleared * (cleared - 1) // 2 + cleared * i * 4096
        output_sum = input_sum - cleared_input + 4096 * (7 + i)
        lines.append('[fixture-step] ' + json.dumps(dict(
            id=i, pid=PID, tid=TID, input_checksum=input_sum,
            output_checksum=output_sum, expected_checksum=output_sum,
            mismatches=0, passed=True, h2d_error=0, launch_error=0, d2h_error=0,
            memset_bytes=memset_bytes, memset_error=0)))
    lines.append('[fixture-ready] ' + json.dumps(dict(pid=PID, tid=TID,
        completed_steps=32, profiler_start_error=0, profiler_stop_error=0,
        final_sync_error=0, passed=True)))
    lines.append('[fixture-exit] ' + json.dumps(dict(pid=PID, tid=TID,
                                                   released=True, returncode=0)))
    receipt.write_text('\n'.join(lines) + '\n')
    return database, receipt


class FixtureAuditTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database, self.receipt = make_fixture(self.root)

    def change(self, sql, args=()):
        with sqlite3.connect(self.database) as db:
            db.execute(sql, args)

    def result(self):
        return subject.audit(self.database, self.receipt, PID)

    def assert_rejected(self):
        self.assertFalse(self.result()['fixture_contract_passed'])

    def test_complete_known_work(self):
        result = self.result()
        self.assertTrue(result['fixture_contract_passed'], result)
        self.assertTrue(result['diagnostics']['valid'])
        self.assertEqual(result['counts']['mapped_steps'], 32)
        self.assertTrue(result['gpu_memory_coverage_complete'])
        self.assertEqual(result['memory_coverage']['known_nonzero_memset_apis'], 32)
        self.assertFalse(result['model_trace_validated'])

    def test_missing_entire_api_kernel_pair_not_hidden_by_bijection(self):
        self.change('DELETE FROM CUPTI_ACTIVITY_KIND_RUNTIME WHERE correlationId=1002')
        self.change('DELETE FROM CUPTI_ACTIVITY_KIND_KERNEL WHERE correlationId=1002')
        self.assert_rejected()

    def test_missing_last_nvtx(self):
        self.change("DELETE FROM NVTX_EVENTS WHERE text='k3fixture|step=31'")
        self.assert_rejected()

    def test_unknown_or_error_diagnostic_fails(self):
        for severity in (0, 3, 99):
            with self.subTest(severity=severity):
                self.change('DELETE FROM DIAGNOSTIC_EVENT')
                self.change('INSERT INTO DIAGNOSTIC_EVENT VALUES(0,1,1,?,?,?)',
                            (severity, 'unclassified collector fault', GLOBAL_PID))
                self.assert_rejected()

    def test_wrong_activity_pid(self):
        self.change('UPDATE CUPTI_ACTIVITY_KIND_KERNEL SET globalPid=? WHERE correlationId=1002',
                    ((PID + 1) << 24,))
        self.assert_rejected()

    def test_unmatched_api(self):
        self.change('INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES(1,2,?,44444,1,0)',
                    (GLOBAL_TID,))
        self.assert_rejected()

    def test_copy_bytes_direction_and_correlation(self):
        for field, value in [('bytes', 8192), ('copyKind', 2), ('correlationId', 55555)]:
            with self.subTest(field=field):
                self.change('UPDATE CUPTI_ACTIVITY_KIND_MEMCPY SET ' + field + '=? WHERE rowid=1',
                            (value,))
                self.assert_rejected()
                self.change('UPDATE CUPTI_ACTIVITY_KIND_MEMCPY SET bytes=16384,copyKind=1,correlationId=1000 WHERE rowid=1')

    def test_host_checksum_is_independent(self):
        text = self.receipt.read_text().splitlines()
        row = json.loads(text[1].split('] ', 1)[1])
        row['output_checksum'] = row['expected_checksum'] = 1
        text[1] = '[fixture-step] ' + json.dumps(row)
        self.receipt.write_text('\n'.join(text) + '\n')
        self.assert_rejected()

    def test_open_range_and_wrong_tid(self):
        for sql in ["UPDATE NVTX_EVENTS SET end=NULL WHERE text='k3fixture|step=31'",
                    "UPDATE NVTX_EVENTS SET globalTid=globalTid+1 WHERE text='k3fixture|step=31'"]:
            with self.subTest(sql=sql):
                self.change(sql)
                self.assert_rejected()
                self.change("UPDATE NVTX_EVENTS SET end=31500,globalTid=? WHERE text='k3fixture|step=31'",
                            (GLOBAL_TID,))

    def test_nonzero_api_error(self):
        self.change('UPDATE CUPTI_ACTIVITY_KIND_RUNTIME SET returnValue=1 WHERE correlationId=1002')
        self.assert_rejected()

    def test_memset_observability_and_known_argument_contract(self):
        for bytes_, correlation in [(512, 1001), (16384, 1005)]:
            with self.subTest(missing_bytes=bytes_):
                with sqlite3.connect(self.database) as db:
                    row = db.execute('SELECT * FROM CUPTI_ACTIVITY_KIND_MEMSET WHERE correlationId=?',
                                     (correlation,)).fetchone()
                    db.execute('DELETE FROM CUPTI_ACTIVITY_KIND_MEMSET WHERE correlationId=?', (correlation,))
                result = self.result()
                self.assertTrue(result['fixture_contract_passed'], result)
                self.assertFalse(result['gpu_memory_coverage_complete'])
                self.assertEqual(result['memory_coverage']['status'], 'UNKNOWN_CAPABILITY')
                self.assertEqual(result['memory_coverage']['unmatched_by_bytes'], {str(bytes_): 1})
                with sqlite3.connect(self.database) as db:
                    db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_MEMSET VALUES(?,?,?,?,?,?,?,?,?)', row)
        for field, value in [('bytes', 0), ('value', 1), ('correlationId', 123456),
                             ('globalPid', (PID + 1) << 24)]:
            with self.subTest(invalid_field=field):
                self.change('UPDATE CUPTI_ACTIVITY_KIND_MEMSET SET ' + field + '=? WHERE correlationId=1001',
                            (value,))
                self.assert_rejected()
                self.change('UPDATE CUPTI_ACTIVITY_KIND_MEMSET SET bytes=512,value=0,correlationId=1001,globalPid=? WHERE correlationId IN(1001,123456)',
                            (GLOBAL_PID,))
        self.change('DROP TABLE CUPTI_ACTIVITY_KIND_MEMSET')
        result = self.result()
        self.assertTrue(result['fixture_contract_passed'], result)
        self.assertFalse(result['gpu_memory_coverage_complete'])
        self.assertEqual(result['memory_coverage']['unmatched_by_bytes'], {'512': 16, '16384': 16})

    def test_warning_and_auxiliary_um_retained_without_model_permission(self):
        self.change('INSERT INTO DIAGNOSTIC_EVENT VALUES(0,2,1,2,?,?)',
                    ('Not all CUDA events might have been collected.', GLOBAL_PID))
        self.change('INSERT INTO DIAGNOSTIC_EVENT VALUES(1,2,1,3,?,?)',
                    ('CUDA device 0: Unified Memory trace is not supported by the current driver version or configuration.', GLOBAL_PID))
        result = self.result()
        self.assertTrue(result['fixture_contract_passed'], result)
        self.assertFalse(result['diagnostics']['valid'])
        self.assertEqual(len(result['diagnostics']['actual_target_warnings']), 1)
        self.assertEqual(len(result['diagnostics']['unavailable_auxiliary_features']), 1)
        self.assertFalse(result['model_trace_validated'])

    def test_offline_negative_derivatives_preserve_original(self):
        before = subject.sha(self.database)
        result = subject.negative_copies(self.database, self.receipt, self.root / 'negative', PID)
        self.assertEqual(subject.sha(self.database), before)
        self.assertEqual(len(result['derivatives']), 2)
        self.assertTrue(all(r['expected_rejected'] for r in result['derivatives'].values()))

    def test_external_pid_binding(self):
        self.assertFalse(subject.audit(self.database, self.receipt, PID + 1)['fixture_contract_passed'])


if __name__ == '__main__':
    unittest.main()
