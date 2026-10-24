"""Eight deterministic offline contracts. No CUDA, model, HTTP, or sampling.

Run only after the stage owner freezes this file and analyze_gpu.py. All mutable
SQLite fixtures and scratch work stay in a newly owned .q4t-work directory.
"""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

MODULE = Path(__file__).with_name('analyze_gpu.py')
spec = importlib.util.spec_from_file_location('q4t_gpu_auditor_under_test', MODULE)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
PID = 42
TID = (PID << 24) | 7
GPID = PID << 24


def make_database(path):
    db = sqlite3.connect(path)
    db.executescript('''
      CREATE TABLE StringIds(id INTEGER PRIMARY KEY,value TEXT);
      CREATE TABLE ENUM_DIAGNOSTIC_SEVERITY_LEVEL(id INT,name TEXT);
      CREATE TABLE DIAGNOSTIC_EVENT(severity INT,text TEXT,globalPid INT);
      CREATE TABLE NVTX_EVENTS(start INT,end INT,globalTid INT,eventType INT,
                               text TEXT,textId INT,endGlobalTid INT);
      CREATE TABLE CUPTI_ACTIVITY_KIND_RUNTIME(start INT,end INT,globalTid INT,
        correlationId INT,nameId INT,returnValue INT,callchainId INT);
      CREATE TABLE CUPTI_ACTIVITY_KIND_KERNEL(start INT,end INT,globalPid INT,
        correlationId INT,deviceId INT,contextId INT,streamId INT,
        shortName INT,demangledName INT,gridX INT,gridY INT,gridZ INT,
        blockX INT,blockY INT,blockZ INT);
      CREATE TABLE CUPTI_ACTIVITY_KIND_MEMCPY(start INT,end INT,globalPid INT,
        correlationId INT,deviceId INT,contextId INT,streamId INT,bytes INT,
        copyKind INT,srcKind INT,dstKind INT);
      CREATE TABLE CUPTI_ACTIVITY_KIND_MEMSET(start INT,end INT,globalPid INT,
        correlationId INT,deviceId INT,contextId INT,streamId INT,bytes INT,
        memKind INT,value INT);
      CREATE TABLE ThreadNames(nameId INT,globalTid INT);
      CREATE TABLE PROFILER_OVERHEAD(start INT,end INT,globalTid INT,
                                    nameId INT,overheadType INT);
    ''')
    db.executemany('INSERT INTO StringIds VALUES(?,?)', [
        (1,'cudaMemcpyAsync_v3020'),(2,'cudaLaunchKernel_v7000'),
        (3,'cudaMemsetAsync_v3020'),(4,'KnownKernel'),
        (5,'CUPTI worker thread'),(6,'CUPTI Buffer flush')])
    db.executemany('INSERT INTO ENUM_DIAGNOSTIC_SEVERITY_LEVEL VALUES(?,?)',
                   [(1,'Info'),(2,'Warning'),(3,'Error')])
    db.executemany('INSERT INTO NVTX_EVENTS VALUES(?,?,?,?,?,?,?)', [
        (10,900,TID,59,'k3fixture|root',None,None),
        (20,500,TID,59,'k3fixture|step=0',None,None)])
    db.executemany('INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES(?,?,?,?,?,?,?)',[
        (30,50,TID,1,1,0,None),(60,100,TID,2,2,0,None),
        (110,160,TID,3,1,0,None),(170,190,TID,4,3,0,None)])
    db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_KERNEL VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (80,200,GPID,2,0,1,7,4,4,1,1,1,32,1,1))
    db.executemany('INSERT INTO CUPTI_ACTIVITY_KIND_MEMCPY VALUES(?,?,?,?,?,?,?,?,?,?,?)',[
        (40,70,GPID,1,0,1,7,16,1,0,1),(150,250,GPID,3,0,1,8,16,2,1,0)])
    db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_MEMSET VALUES(?,?,?,?,?,?,?,?,?,?)',
        (180,210,GPID,4,0,1,8,16,1,0))
    db.commit();db.close()


def cpu_log():
    lines=[f'[exec-capture] event=begin pid={PID} tid=7 input_tokens=8 error=0']
    for batch,phase,p,t,begin,end,cpu_begin in [
            (1,'target_prefill_call',0,8,1000015,1000595,0),
            (2,'mtp.step',8,4,1001110,1001690,400)]:
        lines.append(f'[exec-batch] pid={PID} tid=7 batch={batch} label=mtp.multi seq=0 p={p} t={t} records=1 capacity=4096 bytes=4096 overflow=0 clock_error=0 complete=1')
        lines.append(f'[exec-span] pid={PID} tid=7 batch={batch} id=0 parent=-1 phase={phase} seq=0 p={p} t={t} wall_begin_ns={begin} wall_end_ns={end} cpu_begin_ns={cpu_begin} cpu_end_ns={cpu_begin+300} closed=1 value0=0 value1=0 value2=0')
        lines.append(f'[exec-flush] batch={batch} wall_ns=10 cpu_ns=9 clock_error=0')
    lines.append(f'[exec-capture] event=end pid={PID} tid=7 input_tokens=8 error=0')
    return '\n'.join(lines)+'\n'


class GpuAuditContracts(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[2]/'.q4t-work'
        root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='nsys-offline-contract-',dir=root)
        self.base=Path(self.tmp.name)
        self.counter=0

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self, mutation=None):
        self.counter+=1
        path=self.base/f'{self.counter}.sqlite'
        make_database(path)
        if mutation:
            with sqlite3.connect(path) as db:mutation(db)
        return path

    def run_audit(self,path,**kwargs):
        return audit.audit_database(path,PID,scratch_dir=self.base,mode=kwargs.pop('mode','fixture'),**kwargs)

    def test_01_known_overlap_signed_lag_and_source_immutable(self):
        path=self.fixture(); before=audit.sha(path)
        r=self.run_audit(path)
        self.assertEqual(r['trace_integrity'],'PASS')
        self.assertEqual(r['gpu_all']['sum_ns'],280)
        self.assertEqual(r['gpu_all']['union_ns'],200)
        self.assertEqual(r['gpu_all']['interval_hull_ns'],210)
        self.assertEqual(r['gpu_all']['internal_activity_gap_ns'],10)
        self.assertEqual(r['groups']['k3fixture|step=0']['launch_lag']['negative_return_count'],1)
        self.assertEqual(audit.sha(path),before)
        self.assertFalse(r['stage_environment_verified'])
        self.assertFalse(r['performance_acceptance'])

    def test_02_bidirectional_kernel_copy_memset_loss(self):
        for table,condition,kind in [
            ('CUPTI_ACTIVITY_KIND_KERNEL','1','application_launch_without_kernel'),
            ('CUPTI_ACTIVITY_KIND_MEMCPY','correlationId=1','application_memory_submit_without_activity'),
            ('CUPTI_ACTIVITY_KIND_MEMSET','1','application_memory_submit_without_activity'),
            ('CUPTI_ACTIVITY_KIND_RUNTIME','correlationId=2','gpu_api_missing_or_ambiguous')]:
            with self.subTest(table=table,condition=condition):
                p=self.fixture(lambda db:db.execute('DELETE FROM '+table+' WHERE '+condition))
                r=self.run_audit(p)
                expected=('UNKNOWN_CAPABILITY' if
                    kind=='application_memory_submit_without_activity' else 'FAIL')
                self.assertEqual(r['trace_integrity'],expected)
                self.assertIn(kind,{x['kind'] for x in r['coverage_findings']})

    def test_03_correlation_ambiguity_and_process_isolation(self):
        def duplicate(db,tid):
            db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES(?,?,?,?,?,?,?)',
                       (60,100,tid,2,2,0,None))
        bad=self.run_audit(self.fixture(lambda db:duplicate(db,TID)))
        self.assertEqual(bad['trace_integrity'],'FAIL')
        other=self.run_audit(self.fixture(lambda db:duplicate(db,((PID+1)<<24)|7)))
        self.assertEqual(other['trace_integrity'],'PASS')

    def test_04_helper_requires_exact_flush_and_nonapplication_thread(self):
        def add(db,container,app=False,other_nvtx=False):
            tid=TID if app else (PID<<24)|8
            db.execute('INSERT INTO ThreadNames VALUES(?,?)',(5,tid))
            db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES(?,?,?,?,?,?,?)',
                       (600,620,tid,77,2,0,None))
            if container:
                db.execute('INSERT INTO PROFILER_OVERHEAD VALUES(?,?,?,?,?)',(590,630,tid,6,3))
            if other_nvtx:
                db.execute('INSERT INTO NVTX_EVENTS VALUES(?,?,?,?,?,?,?)',
                           (590,630,tid,59,'unrelated_application_label',None,None))
        for has_container,app,expected in [(False,False,'FAIL'),(True,False,'PASS'),(True,True,'FAIL')]:
            with self.subTest(container=has_container,app=app):
                r=self.run_audit(self.fixture(lambda db:add(db,has_container,app)))
                self.assertEqual(r['trace_integrity'],expected)
                self.assertEqual(len(r['helper_api_evidence']),int(expected=='PASS'))
        r=self.run_audit(self.fixture(lambda db:add(db,True,other_nvtx=True)))
        self.assertEqual(r['trace_integrity'],'FAIL')

    def test_05_warning_subset_only_bound_empty_wrapper(self):
        wrapper=PID+2
        def warnings(db,pid,partial=False,active=False,unknown=False):
            values=sorted(audit.WRAPPER_WARNINGS)
            for text in values[:1] if partial else values:
                db.execute('INSERT INTO DIAGNOSTIC_EVENT VALUES(?,?,?)',(2,text,pid<<24))
            if unknown:
                db.execute('INSERT INTO DIAGNOSTIC_EVENT VALUES(?,?,?)',(2,'Unknown warning',pid<<24))
            if active:
                db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES(?,?,?,?,?,?,?)',
                           (1,2,(pid<<24)|9,100,1,0,None))
        for pid,partial,active,unknown,expected in [(wrapper,False,False,False,'PASS'),
                (wrapper,True,False,False,'PASS'),(wrapper,False,True,False,'FAIL'),
                (PID,False,False,False,'FAIL'),(wrapper+1,True,False,False,'FAIL'),
                (wrapper,True,False,True,'FAIL')]:
            with self.subTest(pid=pid,partial=partial,active=active,unknown=unknown):
                r=self.run_audit(self.fixture(lambda db:warnings(db,pid,partial,active,unknown)),wrapper_pid=wrapper)
                self.assertEqual(r['trace_integrity'],expected)
        self.assertEqual(self.run_audit(self.fixture(),wrapper_pid=wrapper)['trace_integrity'],'PASS')
        def active_without_warning(db):
            db.execute('INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES(?,?,?,?,?,?,?)',
                       (1,2,(wrapper<<24)|9,100,1,0,None))
        self.assertEqual(self.run_audit(self.fixture(active_without_warning),wrapper_pid=wrapper)['trace_integrity'],'FAIL')
        def auxiliary_warning(db):
            db.execute('INSERT INTO DIAGNOSTIC_EVENT VALUES(?,?,?)',
                (2,'CUDA device 0: Unified Memory trace is not supported by the current driver version or configuration.',GPID))
        self.assertEqual(self.run_audit(self.fixture(auxiliary_warning))['trace_integrity'],'FAIL')
        def duplicate_warning(db):
            text=sorted(audit.WRAPPER_WARNINGS)[0]
            db.executemany('INSERT INTO DIAGNOSTIC_EVENT VALUES(?,?,?)',[(2,text,wrapper<<24)]*2)
        self.assertEqual(self.run_audit(self.fixture(duplicate_warning),wrapper_pid=wrapper)['trace_integrity'],'FAIL')
        for severity in ('UnknownLevel', 'Other'):
            with self.subTest(severity=severity):
                def unknown_severity(db):
                    db.execute('INSERT INTO ENUM_DIAGNOSTIC_SEVERITY_LEVEL VALUES(?,?)',(4,severity))
                    db.execute('INSERT INTO DIAGNOSTIC_EVENT VALUES(?,?,?)',(4,'Unclassified diagnostic',GPID))
                self.assertEqual(self.run_audit(self.fixture(unknown_severity))['trace_integrity'],'FAIL')

    def test_06_unclosed_nvtx_and_gpu_before_api_rejected(self):
        bad=self.fixture(lambda db:db.execute('UPDATE NVTX_EVENTS SET end=NULL WHERE text LIKE ?',( '%step%',)))
        with self.assertRaises(ValueError):self.run_audit(bad)
        early=self.fixture(lambda db:db.execute('UPDATE CUPTI_ACTIVITY_KIND_KERNEL SET start=59'))
        r=self.run_audit(early)
        self.assertEqual(r['trace_integrity'],'FAIL')
        self.assertIn('gpu_before_api',{x['kind'] for x in r['coverage_findings']})

    def model_fixture(self):
        def change(db):
            db.execute('DELETE FROM NVTX_EVENTS')
            db.executemany('INSERT INTO NVTX_EVENTS VALUES(?,?,?,?,?,?,?)',[
                (10,600,TID,59,'q4t|1|0|target_prefill_call|seq=0|p=0|t=8',None,None),
                (1000,1600,TID,59,'q4t|2|0|mtp.step|seq=0|p=8|t=4',None,None)])
        return self.fixture(change)

    def test_07_empty_common_offset_preserves_keyed_attribution(self):
        r=self.run_audit(self.model_fixture(),mode='model',server_text=cpu_log())
        self.assertEqual(r['trace_integrity'],'PASS')
        self.assertFalse(r['monotonic_minus_nsight_offset_bounds']['common_intersection_exists'])
        self.assertFalse(r['clock_alignment_used'])
        self.assertEqual(r['coverage']['cpu_spans'],2)
        self.assertEqual(r['gpu_all']['union_ns'],200)

    def test_08_cpu_identity_missing_records_and_bad_return_rejected(self):
        for text in (cpu_log().replace('phase=mtp.step seq=0 p=8','phase=mtp.step seq=0 p=9'),
                     cpu_log().replace('records=1 capacity','records=2 capacity'),
                     cpu_log().replace('overflow=0','overflow=1')):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self.run_audit(self.model_fixture(),mode='model',server_text=text)
        bad=self.fixture(lambda db:db.execute('UPDATE CUPTI_ACTIVITY_KIND_RUNTIME SET returnValue=999 WHERE correlationId=2'))
        r=self.run_audit(bad)
        self.assertEqual(r['trace_integrity'],'FAIL')
        self.assertEqual(r['api_errors'][0]['code'],999)
        unknown=self.model_fixture()
        with sqlite3.connect(unknown) as db:
            db.execute('DELETE FROM CUPTI_ACTIVITY_KIND_MEMSET')
        r=self.run_audit(unknown,mode='model',server_text=cpu_log())
        self.assertEqual(r['trace_integrity'],'UNKNOWN_CAPABILITY')
        self.assertEqual(r['coverage_findings'][0]['status'],'UNKNOWN_CAPABILITY')


if __name__=='__main__':
    unittest.main()
