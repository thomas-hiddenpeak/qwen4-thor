"""Sixteen CPU contracts for the bounded offline parameter analyzer; no CUDA."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import mechanism_analyze as m


PID, TID = 1234, 1234


def records(mode='cupti'):
    out = [('config', dict(pid=PID, tid=TID, mode=mode, **m.FIXED))]
    for name in ('cudart', 'cublasLt', 'cupti_linked'):
        out.append(('event', dict(event='library', name=name, path='/synthetic/' + name,
                                 device=1, inode=2, bytes=3, mtime_ns=4)))
    for ordinal, phase in enumerate(m.PHASES):
        out.append(('event', dict(event='phase_begin', phase=phase, ordinal=ordinal)))
        if phase in m.GEMMS:
            out.append(('event', dict(event='gemm_return', phase=phase, status=0, has_algo=True)))
            out.append(('event', dict(event='algo', phase=phase, key=dict(m.FIXED, out_bits=0),
                                     raw_hex='00112233', attrs=[dict(name='id', status=0, value=7)],
                                     cache_before=phase == 'gemm_cached', cache_after=True)))
        expected = ([165] * 512 if phase == 'control_zero' else [90] * 512
                    if phase == 'control_nonzero' else
                    [m.bf16_word((n % 7 - 3) * (r % 5 + 1) * (r + 1))
                     for r in range(4) for n in range(48)])
        out.append(('event', dict(event='check', phase=phase, passed=True, mismatches=0,
                                 expected_words=expected, actual_words=list(expected))))
        out.append(('event', dict(event='phase_end', phase=phase, ordinal=ordinal)))
    callbacks, activity = [], []
    sequence = 0

    def api(phase, name, corr, domain='runtime', count=0, parent=0, gpu_kind=None, gpu_bytes=0):
        nonlocal sequence
        sequence += 1
        enter = dict(event='api', seq=sequence, enter_seq=sequence, domain=domain, site='enter',
                     name=name, pid=PID, tid=TID, correlation_id=corr, phase=phase,
                     parent_runtime_correlation_id=parent, context_uid=1, cbid=corr,
                     retval=-1, is_memset=name.startswith('cudaMemsetAsync'), count=count,
                     dev_ptr=4096, value=90, stream=8192)
        callbacks.append(enter)
        nested = None
        if name == 'cudaMemsetAsync' and count:
            sequence += 1
            nested = dict(enter, seq=sequence, enter_seq=sequence, domain='driver',
                          name='cuMemsetD8Async', correlation_id=corr + 1000, cbid=corr + 1000,
                          parent_runtime_correlation_id=corr, is_memset=False, count=0)
            callbacks.append(nested)
            sequence += 1
            callbacks.append(dict(nested, seq=sequence, site='exit', retval=0))
        sequence += 1
        callbacks.append(dict(enter, seq=sequence, site='exit', retval=0))
        activity.append(dict(event='activity', kind=domain, name=name + '_v3020', correlation_id=corr,
                             start=corr * 100, end=corr * 100 + 10, process_id=PID,
                             process_id_source='activity_record', thread_id=TID, context_id=1,
                             device_id=0, stream_id=1, runtime_correlation_id=None, cbid=corr,
                             return_value=0, bytes=0, value=0))
        if nested:
            activity.append(dict(activity[-1], kind='driver', name='cuMemsetD8Async_v3020',
                                 correlation_id=corr + 1000, cbid=corr + 1000,
                                 start=corr * 100 + 2, end=corr * 100 + 8))
        if gpu_kind:
            activity.append(dict(event='activity', kind=gpu_kind,
                                 name=m.FAMILY if gpu_kind == 'kernel' else '',
                                 correlation_id=corr + 1000 if nested else corr,
                                 start=corr * 100 + 12, end=corr * 100 + 20, process_id=PID,
                                 process_id_source='collector_process', thread_id=0, context_id=1,
                                 device_id=0, stream_id=1,
                                 runtime_correlation_id=corr if gpu_kind == 'copy' else None,
                                 bytes=gpu_bytes, value=90 if gpu_kind == 'memset' else 0,
                                 cbid=0, return_value=-1))

    if mode == 'cupti':
        for ordinal, phase in enumerate(m.PHASES):
            base = ordinal * 10 + 1
            api(phase, 'cudaMemsetAsync', base, count=512 if ordinal == 1 else 0,
                gpu_kind='memset' if ordinal == 1 else None, gpu_bytes=512)
            if phase in m.GEMMS:
                api(phase, 'cuLaunchKernel', base + 1, domain='driver', gpu_kind='kernel')
            api(phase, 'cudaMemcpy', base + 2, gpu_kind='copy',
                gpu_bytes=512 if ordinal < 2 else 384)
        for row in activity:
            sequence += 1
            row['seq'] = sequence
        out.extend(('event', r) for r in callbacks + activity)
    out.append(('event', dict(event='summary', mode=mode, api_enter_count=len(callbacks) // 2,
                             api_exit_count=len(callbacks) // 2, activity_count=len(activity),
                             drops=0, overflow=0, errors=0, flushed=mode == 'cupti',
                             subscriber_used=mode == 'cupti', record_count=len(callbacks) + len(activity),
                             buffer_requests=1 if mode == 'cupti' else 0,
                             buffer_completions=1 if mode == 'cupti' else 0, buffers_outstanding=0,
                             collector_limit_bytes=m.MAX_LOG, collector_owned_bytes=1024,
                             configured_cupti_buffer_bytes=1024)))
    out.append(('ready', dict(pid=PID, tid=TID, mode=mode, passed=True, gemm_calls=2, controls=2,
                             profiler_start_error=0, profiler_stop_error=0, final_sync_error=0,
                             **m.FIXED)))
    out.append(('exit', dict(pid=PID, tid=TID, released=True, returncode=0)))
    return out


def write_log(path, items):
    path.write_text(''.join('[mechanism-' + kind + '] ' + json.dumps(row) + '\n'
                            for kind, row in items))


def resequence(items):
    """Keep independent callback entry references while ordering raw records."""
    entry_map = {}
    sequence = 0
    for _, row in items:
        if row.get('event') not in ('api', 'activity'):
            continue
        sequence += 1
        if row.get('site') == 'enter':
            entry_map[row['enter_seq']] = sequence
        row['seq'] = sequence
    for _, row in items:
        if row.get('event') == 'api':
            row['enter_seq'] = entry_map[row['enter_seq']]
    summary = next(r for _, r in items if r.get('event') == 'summary')
    summary.update(api_enter_count=sum(r.get('site') == 'enter' for _, r in items),
                   api_exit_count=sum(r.get('site') == 'exit' for _, r in items),
                   activity_count=sum(r.get('event') == 'activity' for _, r in items),
                   record_count=sequence)
    return items


def shared_driver_records(cbids=(304, 303), *, overlap=False, activities=False):
    items = records()
    index = next(i for i, (_, r) in enumerate(items)
                 if r.get('site') == 'enter' and r.get('correlation_id') == 1)
    parent = items[index][1]
    added, exits = [], []
    for ordinal, cbid in enumerate(cbids):
        entry = -ordinal - 1
        row = dict(parent, seq=entry, enter_seq=entry, domain='driver',
                   cbid=cbid, name='cuShared' + str(cbid), is_memset=False,
                   count=0, parent_runtime_correlation_id=1)
        end = dict(row, site='exit', retval=0)
        added.append(('event', row))
        if overlap:
            exits.insert(0, ('event', end))
        else:
            added.append(('event', end))
    items[index + 1:index + 1] = added + exits
    if activities:
        template = next(r for _, r in items if r.get('kind') == 'runtime' and
                        r.get('correlation_id') == 1)
        index = next(i for i, (_, r) in enumerate(items) if r.get('event') == 'summary')
        items[index:index] = [('event', dict(template, kind='driver', cbid=cbid,
                                             name='cuShared' + str(cbid) + '_v3020'))
                              for cbid in dict.fromkeys(cbids)]
    return resequence(items)


def sql_fixture(path):
    db = sqlite3.connect(path)
    db.executescript('''
      CREATE TABLE StringIds(id INTEGER,value TEXT);
      CREATE TABLE META_DATA_EXPORT(name TEXT,value TEXT);
      CREATE TABLE ENUM_DIAGNOSTIC_SEVERITY_LEVEL(id INTEGER,name TEXT);
      CREATE TABLE DIAGNOSTIC_EVENT(severity INTEGER,text TEXT,globalPid INTEGER);
      CREATE TABLE NVTX_EVENTS(start INTEGER,end INTEGER,globalTid INTEGER,eventType INTEGER,
        text TEXT,textId INTEGER,endGlobalTid INTEGER);
      CREATE TABLE CUPTI_ACTIVITY_KIND_RUNTIME(start INTEGER,end INTEGER,globalTid INTEGER,
        correlationId INTEGER,nameId INTEGER,returnValue INTEGER,callchainId INTEGER,eventClass INTEGER);
      CREATE TABLE CUPTI_ACTIVITY_KIND_KERNEL(start INTEGER,end INTEGER,globalPid INTEGER,
        correlationId INTEGER,deviceId INTEGER,contextId INTEGER,streamId INTEGER,
        demangledName INTEGER,mangledName INTEGER,shortName INTEGER);
      CREATE TABLE CUPTI_ACTIVITY_KIND_MEMCPY(start INTEGER,end INTEGER,globalPid INTEGER,
        correlationId INTEGER,deviceId INTEGER,contextId INTEGER,streamId INTEGER,bytes INTEGER);
      CREATE TABLE CUPTI_ACTIVITY_KIND_MEMSET(start INTEGER,end INTEGER,globalPid INTEGER,
        correlationId INTEGER,deviceId INTEGER,contextId INTEGER,streamId INTEGER,bytes INTEGER,value INTEGER);
    ''')
    names = ['cudaMemsetAsync_v3020', 'cuLaunchKernel', 'cudaMemcpy_v3020', m.FAMILY]
    db.executemany('INSERT INTO StringIds VALUES(?,?)', enumerate(names, 1))
    db.execute('INSERT INTO META_DATA_EXPORT VALUES(?,?)', ('EXPORT_SCHEMA_VERSION', '3.24.18'))
    db.execute('INSERT INTO ENUM_DIAGNOSTIC_SEVERITY_LEVEL VALUES(?,?)', (2, 'Warning'))
    db.execute('INSERT INTO DIAGNOSTIC_EVENT VALUES(?,?,?)',
               (2, 'Not all CUDA events might have been collected.', PID << 24))
    gtid, gpid = (PID << 24) | TID, PID << 24
    db.execute('INSERT INTO NVTX_EVENTS VALUES(?,?,?,?,?,?,?)',
               (1, 1000, gtid, 59, 'mechanism|root', None, None))
    for ordinal, phase in enumerate(m.PHASES):
        base, corr = ordinal * 200 + 20, ordinal * 10 + 1
        db.execute('INSERT INTO NVTX_EVENTS VALUES(?,?,?,?,?,?,?)',
                   (base, base + 150, gtid, 59, 'mechanism|phase=' + phase, None, None))
        db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES(?,?,?,?,?,?,?,?)',
                   (base + 1, base + 5, gtid, corr, 1, 0, None, 0))
        if ordinal == 1:
            db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_MEMSET VALUES(?,?,?,?,?,?,?,?,?)',
                       (base + 6, base + 10, gpid, corr, 0, 1, 1, 512, 90))
        if ordinal >= 2:
            db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES(?,?,?,?,?,?,?,?)',
                       (base + 6, base + 10, gtid, corr + 1, 2, 0, None, 1))
            db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_KERNEL VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (base + 11, base + 20, gpid, corr + 1, 0, 1, 1, 4, 4, 4))
        db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES(?,?,?,?,?,?,?,?)',
                   (base + 30, base + 35, gtid, corr + 2, 3, 0, None, 0))
        db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_MEMCPY VALUES(?,?,?,?,?,?,?,?)',
                   (base + 36, base + 40, gpid, corr + 2, 0, 1, 1, 512 if ordinal < 2 else 384))
    db.commit()
    db.close()


class MechanismContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def analyze(self, items):
        path = self.root / 'cupti.log'
        write_log(path, items)
        return m.analyze_cupti(path)

    def test_explicit_zero_and_known_nonzero_are_separate(self):
        result = self.analyze(records())
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['status'], 'VALID_AND_REPRODUCED')
        self.assertTrue(result['eligible_nsys'])
        self.assertEqual(result['controls']['control_zero']['classification'], 'EXPLICIT_ZERO_NO_ACTIVITY')
        self.assertEqual(result['controls']['control_nonzero']['gpu_bytes'], 512)
        self.assertEqual(result['zero_count_calls'], 3)

    def test_missing_nonzero_or_parameter_or_record_is_not_zero(self):
        for mutation in ('nonzero_activity_missing', 'count_missing', 'exit_missing', 'drop', 'sequence'):
            with self.subTest(mutation=mutation):
                items = records()
                if mutation == 'nonzero_activity_missing':
                    row = next(r for _, r in items if r.get('kind') == 'memset')
                    row['bytes'] = 0
                elif mutation == 'count_missing':
                    del next(r for _, r in items if r.get('site') == 'enter')['count']
                elif mutation == 'exit_missing':
                    items = [(k, r) for k, r in items if not (r.get('site') == 'exit' and r.get('correlation_id') == 1)]
                elif mutation == 'drop':
                    next(r for _, r in items if r.get('event') == 'summary')['drops'] = 1
                else:
                    next(r for _, r in items if r.get('kind') == 'memset')['seq'] = 1
                result = self.analyze(items)
                self.assertEqual(result['status'], 'INVALID')
                self.assertFalse(result['eligible_nsys'])

    def test_identity_duplicate_and_correlation_errors_rejected(self):
        for field, value in (('process_id', PID + 1), ('thread_id', TID + 1), ('correlation_id', 999)):
            with self.subTest(field=field):
                items = records()
                row = next(r for _, r in items if r.get('kind') == 'runtime')
                row[field] = value
                self.assertEqual(self.analyze(items)['status'], 'INVALID')
        items = records()
        exits = [r for _, r in items if r.get('site') == 'exit']
        exits[0]['enter_seq'] = 999
        self.assertEqual(self.analyze(items)['status'], 'INVALID')
        items = records()
        for _, row in items:
            if row.get('name') == 'cuMemsetD8Async':
                row['parent_runtime_correlation_id'] = 999
        self.assertEqual(self.analyze(items)['status'], 'INVALID')

    def test_fixed_output_cache_and_phase_contracts(self):
        for mutation in ('output', 'cache', 'phase', 'budget'):
            with self.subTest(mutation=mutation):
                items = records()
                if mutation == 'output':
                    next(r for _, r in items if r.get('event') == 'check')['actual_words'][0] = 0
                elif mutation == 'cache':
                    next(r for _, r in items if r.get('event') == 'algo')['cache_before'] = True
                elif mutation == 'phase':
                    next(r for _, r in items if r.get('event') == 'phase_begin')['ordinal'] = 3
                else:
                    next(r for _, r in items if r.get('event') == 'summary')['collector_owned_bytes'] = m.MAX_LOG
                self.assertEqual(self.analyze(items)['status'], 'INVALID')

    def test_valid_capture_without_family_reproduction_stops_nsys(self):
        items = records()
        for _, row in items:
            if row.get('kind') == 'kernel':
                row['name'] = 'different_valid_algorithm_kernel'
        result = self.analyze(items)
        self.assertEqual(result['status'], 'NOT_REPRODUCED')
        self.assertTrue(result['local_mechanism_valid'])
        self.assertFalse(result['eligible_nsys'])

    def test_nsys_separate_ids_preserves_warning_without_model_waiver(self):
        cupti = self.analyze(records())
        log, db = self.root / 'nsys.log', self.root / 'trace.sqlite'
        write_log(log, records('nsys'))
        sql_fixture(db)
        result = m.analyze_nsys(db, log, cupti)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['status'], 'LOCAL_MECHANISM_OBSERVED')
        self.assertEqual(len(result['diagnostic_events']), 1)
        self.assertFalse(result['model_qualification'])
        self.assertFalse(result['cross_process_comparison']['correlation_ids_joined'])
        self.assertEqual(result['phase_results']['gemm_first']['memsets'][0]['parameters'],
                         'UNAVAILABLE_IN_NSYS_SQLITE')

    def test_nsys_orphan_nonzero_missing_duplicate_and_algorithm_difference(self):
        cupti = self.analyze(records())
        for index, sql in enumerate((
                'UPDATE CUPTI_ACTIVITY_KIND_KERNEL SET correlationId=999',
                'DELETE FROM CUPTI_ACTIVITY_KIND_MEMSET',
                'INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME SELECT * FROM CUPTI_ACTIVITY_KIND_RUNTIME LIMIT 1')):
            with self.subTest(sql=sql):
                log, path = self.root / 'nsys.log', self.root / ('trace' + str(index) + '.sqlite')
                write_log(log, records('nsys'))
                sql_fixture(path)
                with sqlite3.connect(path) as db:
                    db.execute(sql)
                self.assertEqual(m.analyze_nsys(path, log, cupti)['status'], 'INVALID')
        items = records('nsys')
        for _, row in items:
            if row.get('event') == 'algo':
                row['raw_hex'] = '99887766'
        path = self.root / 'different.sqlite'
        sql_fixture(path)
        write_log(log, items)
        self.assertEqual(m.analyze_nsys(path, log, cupti)['status'], 'NOT_REPRODUCED')

    def test_bounded_input_duplicate_json_and_no_follow(self):
        path = self.root / 'input.log'
        path.write_text('[mechanism-config] {"pid":1,"pid":2}\n')
        self.assertEqual(m.analyze_cupti(path)['status'], 'INVALID')
        write_log(path, records())
        with patch.object(m, 'MAX_LOG', 32):
            self.assertEqual(m.analyze_cupti(path)['status'], 'INVALID')
        link = self.root / 'link.log'
        link.symlink_to(path)
        self.assertEqual(m.analyze_cupti(link)['status'], 'INVALID')

    def test_sequential_shared_driver_correlation_has_distinct_pairs(self):
        result = self.analyze(shared_driver_records())
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['status'], 'VALID_AND_REPRODUCED')
        groups = result['callback_pairing']['shared_driver_correlations']
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['correlation_id'], 1)
        self.assertEqual(len(set(groups[0]['enter_seqs'])), 2)
        self.assertLess(groups[0]['parent_enter_seq'], min(groups[0]['enter_seqs']))

    def test_shared_correlation_activity_uses_unique_cbid(self):
        result = self.analyze(shared_driver_records(activities=True))
        original = self.analyze(records())
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['api_activity_count'], original['api_activity_count'] + 2)
        for phase in ('control_zero', 'control_nonzero'):
            for field in ('count', 'gpu_bytes', 'gpu_activity_count', 'classification'):
                self.assertEqual(result['controls'][phase][field], original['controls'][phase][field])

    def test_callback_instance_identity_and_stack_remain_strict(self):
        for mutation in ('cbid', 'pid', 'tid', 'entry', 'retval', 'reordered_exit'):
            with self.subTest(mutation=mutation):
                items = shared_driver_records()
                children = [r for _, r in items if r.get('domain') == 'driver' and
                            r.get('correlation_id') == 1]
                end = children[1]
                if mutation == 'cbid':
                    end['cbid'] += 1
                elif mutation == 'pid':
                    end['pid'] += 1
                elif mutation == 'tid':
                    end['tid'] += 1
                elif mutation == 'entry':
                    end['enter_seq'] = children[2]['enter_seq']
                elif mutation == 'retval':
                    children[0]['retval'] = 0
                else:
                    first, second = children[1], children[3]
                    first['enter_seq'], second['enter_seq'] = second['enter_seq'], first['enter_seq']
                result = self.analyze(items)
                self.assertEqual(result['status'], 'INVALID')
                self.assertFalse(result['eligible_nsys'])

    def test_correlation_reuse_requires_one_parent_and_no_overlap(self):
        for mutation in ('runtime_reuse', 'different_parent', 'parent_missing', 'overlap'):
            with self.subTest(mutation=mutation):
                items = shared_driver_records(overlap=mutation == 'overlap')
                for _, row in items:
                    if row.get('event') != 'api':
                        continue
                    if mutation == 'runtime_reuse' and row['domain'] == 'runtime' and row['correlation_id'] == 3:
                        row['correlation_id'] = 1
                    elif mutation == 'different_parent' and row['domain'] == 'driver' and row['correlation_id'] == 1011:
                        row['correlation_id'] = 1
                    elif mutation == 'parent_missing' and row['domain'] == 'driver' and row['correlation_id'] == 1:
                        row['parent_runtime_correlation_id'] = 0
                result = self.analyze(items)
                self.assertEqual(result['status'], 'INVALID')
                self.assertFalse(result['eligible_nsys'])

    def test_ambiguous_duplicate_or_bad_return_activity_rejected(self):
        for mutation in ('ambiguous', 'duplicate', 'return'):
            with self.subTest(mutation=mutation):
                items = shared_driver_records((304, 304) if mutation == 'ambiguous' else (304, 303),
                                              activities=True)
                row = next(r for _, r in items if r.get('kind') == 'driver' and
                           r.get('correlation_id') == 1)
                if mutation == 'duplicate':
                    index = next(i for i, (_, r) in enumerate(items) if r.get('event') == 'summary')
                    items.insert(index, ('event', dict(row)))
                    resequence(items)
                elif mutation == 'return':
                    row['return_value'] = 1
                result = self.analyze(items)
                self.assertEqual(result['status'], 'INVALID')
                self.assertFalse(result['eligible_nsys'])

    def test_pthread_and_os_tid_bijection_is_explicitly_inferred(self):
        items = shared_driver_records()
        for _, row in items:
            if row.get('kind') in ('runtime', 'driver'):
                row['thread_id'] = 2367240384
        result = self.analyze(items)
        self.assertEqual(result['errors'], [])
        mapping = result['thread_correspondence']
        self.assertFalse(mapping['directly_sampled_pthread'])
        self.assertEqual(mapping['method'], 'INFERRED_FROM_UNIQUE_API_IDENTITIES')
        self.assertEqual(mapping['mappings'], [dict(os_tid=TID, cupti_tid=2367240384,
                                                   unique_api_matches=result['api_activity_count'])])

    def test_thread_map_rejects_one_to_many_many_to_one_and_invalid_id(self):
        for mutation in ('one_to_many', 'many_to_one', 'invalid_id'):
            with self.subTest(mutation=mutation):
                items = records()
                if mutation == 'many_to_one':
                    for _, row in items:
                        if row.get('event') == 'api' and row['correlation_id'] == 3:
                            row['tid'] = TID + 1
                else:
                    row = next(r for _, r in items if r.get('kind') == 'runtime')
                    row['thread_id'] = TID + 1 if mutation == 'one_to_many' else 0
                result = self.analyze(items)
                self.assertEqual(result['status'], 'INVALID')
                self.assertFalse(result['eligible_nsys'])

    def test_gpu_ownership_stays_unique_after_callback_reuse_fix(self):
        items = shared_driver_records(activities=True)
        kernel = next(r for _, r in items if r.get('kind') == 'kernel')
        kernel['correlation_id'] = 1
        result = self.analyze(items)
        self.assertEqual(result['status'], 'INVALID')
        self.assertIn('GPU activity has no unique process-local API correlation', result['errors'][0])
        self.assertFalse(result['eligible_nsys'])


if __name__ == '__main__':
    unittest.main()
