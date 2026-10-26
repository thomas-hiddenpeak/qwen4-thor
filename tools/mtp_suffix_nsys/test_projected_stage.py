"""Six CPU-only pending-200K projection integration contracts."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import nvlog_stream as codec
import recorded_gpu
import stream_recorded_gpu as streamed
from test_gpu_pair_stage import stage, plan


def pending_plan(base):
    value=plan(base)
    value.update(capture_matrix='pending200_projection_v1',http_requests=4,http_loads=2,
                 stage_timeout_seconds=2400,analysis_per_capture_seconds=600,steps=value['steps'][2:])
    binary=str(base/'codec');value['files'][binary]='f'*64
    value['nvlog_stream']=dict(mode='forensic_six_errors_v1',finish_seconds=10,binary=binary,
                              binary_sha256='f'*64,projection=codec.ACK_PROJECTION)
    value['projection_qualification']={name:str(base/(name+'.json')) for name in
        ('cpu_review','full_replay','prefix_screen','reused_eight_k')}
    value['files'].update({p:'a'*64 for p in value['projection_qualification'].values()})
    return value


class ProjectedStageContracts(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name)

    def remaining(self):
        value=pending_plan(self.base)
        value.update(capture_matrix='remaining200_off_v1',http_requests=2,http_loads=1,
                     stage_timeout_seconds=1800,steps=value['steps'][1:])
        value['steps'][0]['timeout']=1680
        for name in ('saved_on_analysis','saved_on_review'):
            path=str(self.base/(name+'.json'))
            value['projection_qualification'][name]=path;value['files'][path]='a'*64
        return value

    def test_07_only_remaining_off_allowed_after_bound_saved_on(self):
        value=self.remaining();stage.validate(value)
        for change in ({'http_requests':4},{'http_loads':2},{'stage_timeout_seconds':2400},
                       {'steps':pending_plan(self.base)['steps']}):
            with self.subTest(change=change),self.assertRaises(ValueError):stage.validate(value|change)
        value['projection_qualification'].pop('saved_on_analysis')
        with self.assertRaises(ValueError):stage.validate(value)

    def test_08_full_phase_limits_fit_without_runtime_extension(self):
        value=self.remaining();stage.validate(value)
        step=value['steps'][0]
        measurement=step['timeout']-stage.CLEANUP_SECONDS
        required=840+stage.CLEANUP_SECONDS+value['nsys']['export_timeout_seconds']+value['analysis_per_capture_seconds']
        self.assertEqual(measurement,required)
        self.assertLessEqual(step['timeout'],value['stage_timeout_seconds'])
        step['timeout']=900
        with self.assertRaises(ValueError):stage.validate(value)

    def test_01_pending_matrix_rejects_extra_requests_reordering_and_loose_budgets(self):
        value=pending_plan(self.base);stage.validate(value)
        for key,bad in [('http_requests',6),('http_loads',4),('stage_timeout_seconds',2401),
                        ('analysis_per_capture_seconds',601),('source_log_limit_bytes',stage.LOG_LIMIT+1),
                        ('capture_matrix','arbitrary'),('retries',1)]:
            with self.subTest(key=key),self.assertRaises(ValueError):stage.validate(value|{key:bad})
        for steps in (list(reversed(value['steps'])),value['steps'][:1],plan(self.base)['steps']):
            with self.assertRaises(ValueError):stage.validate(value|{'steps':steps})
        value['steps'][0]['cases']=value['steps'][0]['cases'][1:]
        with self.assertRaises(ValueError):stage.validate(value)

    def test_02_projection_requires_frozen_proofs_and_explicit_matrix(self):
        original=pending_plan(self.base)
        for mode in ('missing_proof','unbound_proof','unknown_projection','no_projection','old_matrix'):
            value=copy.deepcopy(original)
            if mode=='missing_proof':value['projection_qualification'].pop('full_replay')
            if mode=='unbound_proof':value['files'].pop(value['projection_qualification']['prefix_screen'])
            if mode=='unknown_projection':value['nvlog_stream']['projection']='drop_all'
            if mode=='no_projection':value.pop('nvlog_stream')
            if mode=='old_matrix':value.pop('capture_matrix')
            with self.subTest(mode=mode),self.assertRaises(ValueError):stage.validate(value)

    def test_03_analysis_command_carries_explicit_view_and_frozen_budget(self):
        value=pending_plan(self.base);child=MagicMock(returncode=0);child.poll.return_value=0
        good=dict(status='FORENSIC_RECORDED_GPU_ONLY',scope_gate_passed=True,errors=[],
                  qualification_passed=False,full_trace_integrity_proven=False,performance_acceptance=False)
        (self.base/'recorded-gpu-audit.json').write_text(json.dumps(good))
        (self.base/'strict-audit.json').write_text('{}')
        def spawn(command,**_):
            self.assertEqual(command[-4:],['--projection',codec.ACK_PROJECTION,'--analysis-seconds','600'])
            return child
        with patch.object(stage.subprocess,'Popen',side_effect=spawn):
            result=stage.analyze_capture(value,self.base,dict(pid=7,wrapper_pid=6),stage.time.monotonic()+700,lambda:None)
        self.assertFalse(result['qualification_passed'])
        self.assertEqual(json.loads((self.base/'analysis-exit.json').read_text())['returncode'],0)

    def test_04_view_receipt_binding_and_explicit_source_metadata(self):
        receipt=dict(projection=codec.ACK_PROJECTION,retained_lines=2)
        terminal={'raw_source':{'lines':2}}
        metadata=dict(mode=codec.ACK_PROJECTION,source_kind='PROJECTED_NVLOG_VIEW',original_bytes_recoverable=False)
        with patch.object(codec,'_json',return_value=receipt), \
             patch.object(codec,'projection_evidence',return_value=metadata), \
             patch.object(codec,'iter_lines',return_value=iter([b'one\n',b'two\n'])), \
             patch.object(recorded_gpu,'terminal_evidence_lines',return_value=terminal) as parse:
            result=streamed.terminal_from_stream(self.base,7,codec.ACK_PROJECTION,600)
            self.assertEqual(result,(terminal,receipt,metadata));self.assertEqual(parse.call_args.kwargs,{'max_seconds':600})
        with patch.object(codec,'_json',side_effect=[receipt,receipt|{'retained_lines':3}]), \
             patch.object(recorded_gpu,'terminal_evidence_lines') as parse:
            with self.assertRaises(ValueError):streamed.terminal_from_stream(self.base,7,codec.ACK_PROJECTION,600)
            parse.assert_not_called()
        with patch.object(codec,'_json',return_value=receipt),patch.object(recorded_gpu,'terminal_evidence_lines') as parse:
            with self.assertRaises(ValueError):streamed.terminal_from_stream(self.base,7,'none',120)
            parse.assert_not_called()

    def test_05_default_deadline_unchanged_and_projection_limit_fails_closed(self):
        before=recorded_gpu.terminal_evidence(b'',7)
        self.assertEqual(before,recorded_gpu.terminal_evidence_lines([],7,max_seconds=600))
        for seconds in (0,True,601,1.5):
            with self.subTest(seconds=seconds),self.assertRaises(ValueError):
                recorded_gpu.terminal_evidence_lines([],7,max_seconds=seconds)
        with patch.object(recorded_gpu.time,'monotonic',side_effect=[0,601]),self.assertRaises(TimeoutError):
            recorded_gpu.terminal_evidence_lines([b'unknown\n'],7,max_seconds=600)
        for projection,seconds in [('none',600),(codec.ACK_PROJECTION,120),('drop_all',600)]:
            with self.assertRaises(ValueError):streamed.analyze(self.base,7,6,projection=projection,analysis_seconds=seconds)

    def test_06_incomplete_terminal_or_new_errors_never_promoted(self):
        import test_stream_recorded_gpu as policy
        terminal=policy.terminal();terminal['explicit_loss']=['loss']
        with self.assertRaises(ValueError):streamed.error_scope(terminal,Path('/owned/trace.1.nsys-rep'))
        terminal=policy.terminal();terminal['qualification_errors'].append('new actual-target error')
        with self.assertRaises(ValueError):streamed.error_scope(terminal,Path('/owned/trace.1.nsys-rep'))
