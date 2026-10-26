"""Eight bounded CPU integration/policy contracts; no GPU or profiler."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import stream_recorded_gpu as m
from test_gpu_pair_stage import stage, plan


def terminal():
    errors=[]
    for n,k in enumerate((33,34,35,36)):
        errors.append(dict(classification='AUXILIARY_ACTIVITY_CONTEXT_DISABLE_REJECTED',
            activity_kind=k,record={'line':4+n},resolution='UNRESOLVED',qualification_allowed=False))
    for n in range(2):
        errors.append(dict(classification='POST_REPORT_DESTRUCTOR_BALANCED_COUNTERS',
            record={'line':14+n},resolution='UNRESOLVED',qualification_allowed=False,
            counters_equal=True,allocated_count=35,returned_count=35,preceding_report_save_count=1))
    return dict(qualification_errors=[m.UNKNOWN],explicit_loss=[],late_filter_pair_observed=True,
        forensic_error_classifications=errors,
        producer_finish={'CUDAFinish':[{'line':1},{'line':12}],
            'ReportCUDADone':[{'line':2},{'line':13}],'NVTXFinish':[{'line':9}]},
        observed_report_save_markers=[dict(line=11,raw='[SaveReport]: Report file saved: /owned/trace.1.nsys-rep')])


class StreamedContracts(unittest.TestCase):
    def test_01_six_errors_remain_unresolved(self):
        value=terminal();before=copy.deepcopy(value)
        result=m.error_scope(value,Path('/owned/trace.1.nsys-rep'))
        self.assertFalse(result['qualification_allowed']);self.assertFalse(result['no_loss_proven'])
        self.assertEqual(value,before)

    def test_02_unknown_duplicate_loss_and_absent_errors_rejected(self):
        for mode in ('new','duplicate','loss','none','missing_filter'):
            value=terminal()
            if mode=='new':value['qualification_errors'].append('new')
            if mode=='duplicate':value['forensic_error_classifications'].append(value['forensic_error_classifications'][0])
            if mode=='loss':value['explicit_loss']=['lost']
            if mode=='none':value['qualification_errors']=[]
            if mode=='missing_filter':value['late_filter_pair_observed']=False
            with self.subTest(mode=mode),self.assertRaises(ValueError):m.error_scope(value,Path('/owned/trace.1.nsys-rep'))

    def test_03_kind_order_and_unique_producer(self):
        for mode in ('kind','early','late','duplicate'):
            value=terminal()
            if mode=='kind':value['forensic_error_classifications'][0]['activity_kind']=32
            if mode=='early':value['forensic_error_classifications'][0]['record']['line']=1
            if mode=='late':value['forensic_error_classifications'][0]['record']['line']=10
            if mode=='duplicate':value['producer_finish']['CUDAFinish'].append({'line':2})
            with self.subTest(mode=mode),self.assertRaises(ValueError):m.error_scope(value,Path('/owned/trace.1.nsys-rep'))

    def test_04_report_identity_counters_and_order(self):
        for mode in ('path','unbalanced','before','multiple'):
            value=terminal()
            if mode=='path':value['observed_report_save_markers'][0]['raw']+='other'
            if mode=='unbalanced':value['forensic_error_classifications'][4]['returned_count']=34
            if mode=='before':value['forensic_error_classifications'][4]['record']['line']=10
            if mode=='multiple':value['observed_report_save_markers']*=2
            with self.subTest(mode=mode),self.assertRaises(ValueError):m.error_scope(value,Path('/owned/trace.1.nsys-rep'))

    def test_05_structure_subcheck_preserves_original_rejection(self):
        value=terminal();before=copy.deepcopy(value)
        def qualify(audit,missing,current,pid):
            if current['qualification_errors']:raise ValueError('original unknown errors')
            if audit.get('missing_kernel'):raise ValueError('missing kernel')
            return {'structural':True}
        with patch.object(m.recorded,'qualify',side_effect=qualify):
            result=m.structure_gate({},[],value,7)
            self.assertFalse(result['original_qualification_passed'])
            with self.assertRaises(ValueError):m.structure_gate({'missing_kernel':True},[],value,7)
        self.assertEqual(value,before)

    def test_06_forensic_result_cannot_promote_acceptance(self):
        good=dict(status='FORENSIC_RECORDED_GPU_ONLY',scope_gate_passed=True,errors=[],
            qualification_passed=False,full_trace_integrity_proven=False,performance_acceptance=False)
        m.require_forensic(good)
        for key in ('qualification_passed','full_trace_integrity_proven','performance_acceptance'):
            with self.subTest(key=key),self.assertRaises(ValueError):m.require_forensic(good|{key:True})

    def test_07_plan_identity_guard_and_compressed_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);value=plan(base);binary=str(base/'codec')
            value['files'][binary]='f'*64
            value['nvlog_stream']=dict(mode='forensic_six_errors_v1',finish_seconds=10,binary=binary,binary_sha256='f'*64)
            stage.validate(value)
            value['nvlog_stream']['binary_sha256']='0'*64
            with self.assertRaises(ValueError):stage.validate(value)
            guard=stage.MeasurementGuard(base,stage.time.monotonic()+10)
            guard.extra_check=MagicMock(side_effect=RuntimeError('worker failed'))
            with self.assertRaises(RuntimeError):guard.check()
            self.assertTrue(guard.failure['environment_abort'])
            (base/'nvlog-stream').mkdir()
            with (base/'nvlog-stream/nvlog.zst').open('wb') as f:f.truncate(stage.LOG_LIMIT+1)
            with self.assertRaises(RuntimeError):stage.check_files(base)

    def test_08_actual_driver_stream_order_failure_and_no_plain_snapshot(self):
        for fail in (False,True):
            with self.subTest(finish_failure=fail),tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
                base=Path(directory);value=plan(base);Path(value['output']).mkdir()
                value['nvlog_stream']=dict(binary=str(base/'codec'),finish_seconds=10,mode='forensic_six_errors_v1')
                output=Path(value['output'])/'nsys8-off';identity=dict(pid=7,start_ticks=19,wrapper_pid=8)
                calls=[];launcher=MagicMock(pid=9,returncode=0)
                session=MagicMock();session.name='owned';session.report_ready_succeeded=True
                collector=MagicMock();collector.start.return_value=collector
                collector.start.side_effect=lambda:(calls.append('reader-ready') or collector)
                def finish(**kwargs):
                    calls.append('eof')
                    if fail:raise TimeoutError('writer still open')
                    return {'status':'VALID'}
                collector.finish.side_effect=finish
                session.start.side_effect=lambda:calls.append('session')
                def launch(_argv,stream,**kwargs):
                    stream.write('serving on port 8764\n');stream.flush();return launcher
                session.launch.side_effect=launch
                session.await_report.side_effect=lambda **kw:calls.append('report')
                def request(*_, client_check=None):
                    self.assertTrue(callable(client_check))
                    with (output/'server.log').open('a') as f:f.write(
                        '[exec-capture] event=begin pid=7 input_tokens=8170 error=0\n'
                        '[exec-capture] event=end pid=7 input_tokens=8170 error=0\n'
                        '[q4t][request] id=r event=end released=1 outcome=success reason=completed\n')
                    return [dict(outputs=['e'*64],responses=[dict(actual_output=256,finish=['length'])])]
                def cleanup(*args):
                    calls.append('writers-exit');self.assertLessEqual(args[-1]-stage.time.monotonic(),50)
                    return {'returncode':0},[]
                def load(name):
                    if name=='nvlog_stream':return SimpleNamespace(Collector=lambda *a,**k:collector)
                    if name=='pair_work_contract':return SimpleNamespace(validate_work=lambda *a:{'status':'VALID','errors':[]})
                    raise AssertionError(name)
                patches=[(stage.legacy,'require_guard',MagicMock()),(stage.sync,'Session',lambda *a,**k:session),
                    (stage,'wait_ready',lambda *a:(identity,99)),(stage,'require_live',MagicMock()),
                    (stage.old.http,'event',MagicMock()),(stage.old.http,'capacity_evidence',lambda *a:{'matches_requested':True}),
                    (stage.old.admission,'run_cases',request),(stage,'loaded_libraries',lambda *a:{}),
                    (stage,'cleanup_owned',cleanup),(stage,'module',load),
                    (stage.MeasurementGuard,'arm',MagicMock()),(stage.MeasurementGuard,'disarm',MagicMock()),
                    (stage.old,'audit_execution',lambda *a:{'complete':True}),
                    (stage.runner,'export_database',lambda *a,**kw:calls.append('export')),
                    (stage,'analyze_capture',lambda *a:calls.append('analysis'))]
                for obj,key,replacement in patches:stack.enter_context(patch.object(obj,key,replacement))
                snapshot=stack.enter_context(patch.object(stage,'snapshot_logs'))
                if fail:
                    with self.assertRaises(stage.legacy.EnvironmentAbort):stage.custom_step(value,'nsys8-off',base/'plan.json')
                    self.assertNotIn('export',calls);self.assertNotIn('analysis',calls)
                else:
                    stage.custom_step(value,'nsys8-off',base/'plan.json')
                    self.assertEqual(calls,['reader-ready','session','report','writers-exit','eof','export','analysis'])
                collector.abort.assert_called_once();snapshot.assert_not_called()
                self.assertIn('nvlog-stream/input.fifo',(output/'nvlog.config').read_text())


if __name__=='__main__':unittest.main()
