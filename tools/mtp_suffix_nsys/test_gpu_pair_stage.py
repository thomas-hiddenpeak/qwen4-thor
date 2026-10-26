"""CPU-only fixed-matrix and owned synchronous lifecycle contracts."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location('gpu_pair_stage_test',
    Path(__file__).with_name('gpu_pair_stage.py'))
stage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage)


def plan(base):
    value = dict(http_requests=6, http_loads=4, stage_timeout_seconds=4200,
        direct_contract_timeout_seconds=0, fixture_runs=0, retries=0, warmups=0,
        observation_build_limit=0, runtime_optimization_candidates=0,
        analysis_per_capture_seconds=120, source_log_limit_bytes=stage.LOG_LIMIT,
        sqlite_limit_bytes=stage.SQLITE_LIMIT, resource_sample_max_age_seconds=5,
        monitor_fix=dict(exit_pending=dict(ttl_seconds=5, capacity=16, poll_timeout_ms=0)),
        nsys=dict(path=str(base/'nsys'), interactive_contract=stage.sync.contract(),
                  export_timeout_seconds=120, launcher_exit_timeout_seconds=30),
        candidate=str(base/'q4t'), candidate_sha256=stage.CANDIDATE_SHA,
        model=str(base/'model'), artifact=str(base/'artifact'), output=str(base/'run'),
        port=8764, nvlog_config='+ global {{ ForceFlush=true; LogFileName="{log}"; }}\n',
        qualification_reuse={str(base/'proof.json'): 'a'*64}, steps=[])
    value['tool_dependency'] = dict(path=value['nsys']['path'], package_root=str(base/'package'),
                                   manifest=str(base/'manifest.json'))
    value['files'] = {value['candidate']: stage.CANDIDATE_SHA,
                      value['nsys']['path']: 'b'*64, **value['qualification_reuse']}
    for name, suffix, tokens, requests in stage.runner.MODEL_STEPS:
        cases = []
        for nominal, actual in [(8192,8170)] + ([(204800,204781)] if requests==2 else []):
            path = str(base/f'context-{nominal}.jsonl')
            value['files'][path] = 'c'*64
            cases.append(dict(id=f'context-{nominal}', path=path, input_tokens=actual,
                input_min=actual-1, input_max=nominal, output_tokens=256, repeats=1,
                role='development_diagnostic', prompt_sha256='d'*64,
                expected_output_sha256='e'*64))
        value['steps'].append(dict(name=name, head='subset', suffix=suffix,
            measurement_role='nsys_profile', execution_diagnostic=True,
            profile_input_tokens=tokens, kind='timing', http_requests=requests,
            http_loads=1, timeout=900, cases=cases,
            command=['/usr/bin/python3','-B',str(Path(stage.__file__).resolve()),
                     '--plan',str(base/'plan.json'),'--custom-step',name]))
    return value


class PairStageContracts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)

    def test_fixed_matrix_rejects_extra_work_and_unbound_identity(self):
        value = plan(self.base)
        stage.validate(value)
        for key in ('http_requests','fixture_runs','warmups','analysis_per_capture_seconds'):
            with self.subTest(key=key):
                changed = copy.deepcopy(value); changed[key] += 1
                with self.assertRaises(ValueError): stage.validate(changed)
        for mode in ('order','predecessor','env','contract','proof','runtime'):
            with self.subTest(mode=mode):
                changed = copy.deepcopy(value)
                if mode=='order': changed['steps'].reverse()
                elif mode=='predecessor': changed['steps'][2]['cases'].pop(0)
                elif mode=='env': changed['steps'][0]['env']={'LD_PRELOAD':'foreign'}
                elif mode=='contract': changed['nsys']['interactive_contract']['schema']=0
                elif mode=='proof': changed['qualification_reuse'][str(self.base/'proof.json')]='bad'
                else: changed['candidate_sha256']='bad'
                with self.assertRaises(ValueError): stage.validate(changed)

    def test_client_environment_removes_injection_and_diagnostics(self):
        with patch.object(stage.runner,'clean_env',return_value=dict(PATH='/bin',
                LD_PRELOAD='bad',CUPTI_ACTIVITY='bad',Q4T_EXEC_DIAGNOSTIC='1')):
            self.assertEqual(stage.clean_env(),{'PATH':'/bin'})

    def test_startup_allows_launcher_zero_but_binds_live_actual_target(self):
        (self.base/'target-identity.json').write_text('{}')
        (self.base/'server.log').write_text('serving on port 8764\n')
        identity = dict(pid=7,start_ticks=19,wrapper_pid=8)
        launcher = MagicMock(); launcher.poll.return_value=0
        with patch.object(stage.runner,'bind_model_target',return_value=identity) as bound, \
                patch.object(stage.os,'pidfd_open',return_value=99), \
                patch.object(stage,'require_live') as live:
            actual,fd=stage.wait_ready(plan(self.base),self.base,MagicMock(),launcher,lambda:None)
        self.assertEqual((actual,fd),(identity,99));bound.assert_called_once()
        live.assert_called_once_with(identity,99)

    def test_startup_rejects_nonzero_or_missing_owned_identity(self):
        (self.base/'server.log').write_text('serving on port 8764\n')
        for code in (1,0):
            with self.subTest(code=code), patch.object(stage.os,'pidfd_open') as opened:
                child=MagicMock();child.poll.return_value=code
                with self.assertRaises(RuntimeError):
                    stage.wait_ready(plan(self.base),self.base,MagicMock(),child,lambda:None)
                opened.assert_not_called()

    def test_actual_target_identity_and_pidfd_both_required(self):
        identity=dict(pid=7,start_ticks=19)
        for current,ready in ((dict(start_ticks=19,state='R'),False),
                              (dict(start_ticks=20,state='R'),False),
                              (dict(start_ticks=19,state='Z'),False),
                              (dict(start_ticks=19,state='R'),True)):
            with self.subTest(current=current,ready=ready), \
                    patch.object(stage.old,'proc_identity',return_value=current), \
                    patch.object(stage.runner,'pidfd_exited',return_value=ready):
                if current['start_ticks']==19 and current['state']=='R' and not ready:
                    stage.require_live(identity,99)
                else:
                    with self.assertRaises(RuntimeError): stage.require_live(identity,99)

        # Exercise the cleanup identity gate independently of startup/reporting.
        for mode in ('normal','expired_measurement','tight_budget','wrong_owner',
                     'wrong_session','changed_target','shutdown_failed','already_attempted','already_exited'):
            with self.subTest(cleanup=mode):
                output=self.base/mode;output.mkdir()
                identity=dict(pid=7,start_ticks=19,wrapper_pid=8)
                stage.save(output/'target-exit.json',dict(pid=7,wrapper_pid=8,returncode=0))
                session=MagicMock();session.name='q4t-suffix-1-2-'+'a'*16+'-owned'
                session.owner=dict(pid=1,start_ticks=2);session.output=output
                session.deadline=50;session._closed=False
                session.check=MagicMock(side_effect=RuntimeError('failed measurement check'))
                measurement_check=session.check
                session.can_shutdown=mode!='already_attempted'
                session.shutdown_attempted=mode=='already_attempted'
                session.shutdown_succeeded=False
                session.reap_adopted.return_value=dict(complete=True,remaining=[],not_children=[])
                if mode=='wrong_owner':session._owner_current.side_effect=RuntimeError('owner changed')
                if mode=='wrong_session':session.name='q4t-suffix-3-2-'+'a'*16+'-owned'
                def shutdown(timeout):
                    session.check()
                    self.assertLessEqual(timeout,5 if mode=='tight_budget' else 15)
                    session.shutdown_attempted=True
                    if mode=='shutdown_failed':raise RuntimeError('shutdown failed')
                    session.shutdown_succeeded=True
                    return dict(returncode=0)
                session.shutdown.side_effect=shutdown
                session.close.side_effect=lambda:setattr(session,'_closed',True)
                launcher=MagicMock(returncode=0);launcher.poll.return_value=0
                state={'exited':mode=='already_exited'}
                def send(fd,signum):
                    self.assertEqual(fd,99);self.assertEqual(signum,stage.signal.SIGTERM)
                    state['exited']=True
                rebound=dict(identity,start_ticks=20) if mode=='changed_target' else identity
                with patch.object(stage.time,'monotonic',return_value=100), \
                        patch.object(stage.runner,'bind_model_target',return_value=rebound) as bind, \
                        patch.object(stage,'require_live') as live, \
                        patch.object(stage.runner,'pidfd_exited',side_effect=lambda *_:state['exited']), \
                        patch.object(stage.signal,'pidfd_send_signal',side_effect=send) as signal_target, \
                        patch.object(stage.os,'close') as close:
                    target,errors=stage.cleanup_owned(plan(self.base),output,session,launcher,
                        identity,99,125 if mode=='tight_budget' else 160)
                measurement_check.assert_not_called();close.assert_called_once_with(99)
                session.close.assert_called_once()
                if mode in ('wrong_owner','wrong_session'):
                    session.shutdown.assert_not_called();bind.assert_not_called()
                    signal_target.assert_not_called();self.assertIsNone(target)
                    session.reap_adopted.assert_not_called()
                elif mode=='changed_target':
                    signal_target.assert_not_called();live.assert_not_called()
                    self.assertIsNone(target)
                elif mode=='already_exited':
                    signal_target.assert_not_called();bind.assert_not_called()
                    self.assertEqual(target['returncode'],0)
                else:
                    signal_target.assert_called_once_with(99,stage.signal.SIGTERM)
                    live.assert_called_once_with(identity,99)
                    self.assertEqual(target['returncode'],0)
                if mode=='already_attempted':session.shutdown.assert_not_called()
                elif mode not in ('wrong_owner','wrong_session'):session.shutdown.assert_called_once()
                self.assertEqual(bool(errors),mode in ('wrong_owner','wrong_session',
                    'changed_target','shutdown_failed','already_attempted'))
                budget=json.loads((output/'cleanup-budget.json').read_text())
                self.assertFalse(budget['measurement_check_used'])
                self.assertEqual(budget['original_measurement_deadline'],50)

    def test_snapshot_adapter_is_isolated_and_keeps_fixed_upper_bound(self):
        original=stage.module('nvlog_snapshot')
        (self.base/'nvlog.log').write_text('I01:00:00.000 x pid=7 tid= 7 normal\n')
        receipt=stage.snapshot_logs(self.base,7)
        self.assertEqual(original.MAX_BYTES,64*1024**2)
        self.assertEqual(receipt['max_bytes'],stage.LOG_LIMIT)
        self.assertTrue(receipt['stage_adapter']['isolated_module'])
        result=json.loads((self.base/'nvlog-reconciliation.json').read_text())
        self.assertTrue(result['exact_prefix']); self.assertFalse(result['warning_waiver'])
        adapter=stage.module('nvlog_snapshot');adapter.MAX_BYTES=stage.LOG_LIMIT
        with self.assertRaises(ValueError): adapter._limit(stage.LOG_LIMIT+1)
        oversized=self.base/'server.log'
        with oversized.open('wb') as stream: stream.truncate(stage.LOG_LIMIT+1)
        with self.assertRaises(RuntimeError): stage.check_files(self.base)

    def test_local_qualification_does_not_require_or_overwrite_strict_pass(self):
        good=dict(qualification_passed=True,errors=[],performance_acceptance=False,
                  strict_trace_integrity='UNKNOWN_CAPABILITY')
        stage.require_qualified(good)
        for key,value in (('qualification_passed',False),('errors',['loss']),
                          ('performance_acceptance',True)):
            with self.subTest(key=key):
                bad=dict(good);bad[key]=value
                with self.assertRaises(RuntimeError):stage.require_qualified(bad)

    def test_analysis_timeout_only_stops_owned_child_and_is_environment_abort(self):
        child=MagicMock(returncode=-15)
        child.poll.return_value=None
        with patch.object(stage.subprocess,'Popen',return_value=child), \
                patch.object(stage.time,'monotonic',return_value=200):
            with self.assertRaises(stage.legacy.EnvironmentAbort):
                stage.analyze_capture(plan(self.base),self.base,
                    dict(pid=7,wrapper_pid=8),100,lambda:None)
        child.terminate.assert_called_once();child.wait.assert_called_once_with(timeout=5)
        self.assertTrue(json.loads((self.base/'analysis-exit.json').read_text())['forced'])

        for mode in ('file_limit','alarm'):
            with self.subTest(first_failure=mode):
                output=self.base/mode;output.mkdir()
                guard=stage.MeasurementGuard(output,110)
                with patch.object(stage.time,'monotonic',return_value=100), \
                        patch.object(stage,'check_files',side_effect=RuntimeError('log limit')):
                    with self.assertRaises((RuntimeError,TimeoutError)):
                        guard.check() if mode=='file_limit' else guard.expired(None,None)
                    original=copy.deepcopy(guard.failure)
                    guard.latch(RuntimeError('later cleanup failure'))
                    with self.assertRaises(RuntimeError):guard.check()
                self.assertEqual(guard.failure,original)
                self.assertTrue(original['environment_abort'])
                self.assertFalse(original['repaired_by_cleanup'])
                self.assertEqual(json.loads((output/'measurement-failure.json').read_text()),original)
        guard=stage.MeasurementGuard(self.base,110)
        with patch.object(stage.time,'monotonic',return_value=100), \
                patch.object(stage.signal,'getitimer',return_value=(0.0,0.0)), \
                patch.object(stage.signal,'getsignal',return_value='old-handler'), \
                patch.object(stage.signal,'signal') as handler, \
                patch.object(stage.signal,'setitimer') as timer:
            guard.arm();guard.disarm();guard.disarm()
        self.assertEqual(timer.call_args_list[0].args,(stage.signal.ITIMER_REAL,10))
        self.assertEqual(timer.call_args_list[1].args,(stage.signal.ITIMER_REAL,0))
        self.assertEqual(timer.call_count,2)
        self.assertEqual(handler.call_args.args,(stage.signal.SIGALRM,'old-handler'))
        with patch.object(stage.signal,'getitimer',return_value=(1.0,0.0)), \
                patch.object(stage.signal,'setitimer') as timer:
            with self.assertRaises(RuntimeError):guard.arm()
            timer.assert_not_called()

    def test_analysis_preserves_strict_failure_and_successful_local_receipt(self):
        strict=dict(trace_integrity='FAIL',diagnostics={'warnings':['original']})
        stage.save(self.base/'strict-audit.json',strict)
        stage.save(self.base/'recorded-gpu-audit.json',dict(
            qualification_passed=True,errors=[],performance_acceptance=False))
        child=MagicMock(returncode=0);child.poll.return_value=0
        with patch.object(stage.subprocess,'Popen',return_value=child):
            result=stage.analyze_capture(plan(self.base),self.base,
                dict(pid=7,wrapper_pid=8),stage.time.monotonic()+20,lambda:None)
        self.assertTrue(result['qualification_passed'])
        self.assertEqual(json.loads((self.base/'strict-audit.json').read_text()),strict)
        child.terminate.assert_not_called()

    def test_normal_capture_orders_http_report_shutdown_exit_export_analysis(self):
        value=plan(self.base);Path(value['output']).mkdir()
        step=value['steps'][0];output=Path(value['output'])/step['name']
        identity=dict(pid=7,start_ticks=19,wrapper_pid=8)
        launcher=MagicMock(pid=9,returncode=0);launcher.poll.return_value=0
        calls=[]
        session=MagicMock(name='session');session.name='q4t-suffix-1-owned'
        session.report_ready_succeeded=True
        session.start.side_effect=lambda:calls.append('start')
        def launch(_argv,stream,envvars):
            calls.append('launch')
            self.assertIn('NVLOG_CONFIG_FILE',envvars)
            stream.write('serving on port 8764\n');stream.flush()
            return launcher
        session.launch.side_effect=launch
        def request(_plan,_step,_output,_launcher,env,_capacity):
            calls.append('http')
            self.assertFalse(any(k.startswith(('Q4T_','NVLOG_')) for k in env))
            with (output/'server.log').open('a') as stream:
                stream.write('[exec-capture] event=begin pid=7 input_tokens=8170 error=0\n'
                    '[exec-capture] event=end pid=7 input_tokens=8170 error=0\n'
                    '[q4t][request] id=r event=end released=1 outcome=success reason=completed\n')
            return [dict(outputs=['e'*64],responses=[dict(actual_output=256,finish=['length'])])]
        def report(**kwargs):
            calls.append('report')
            receipt=kwargs['target_receipt']
            self.assertEqual(receipt['source'],'model-capture-end')
            self.assertEqual(receipt['expected_input_tokens'],8170)
            self.assertLessEqual(kwargs['timeout'],45)
            kwargs['check']()
        session.await_report.side_effect=report
        def cleanup(_plan,_output,_session,_launcher,_identity,_fd,deadline):
            calls.extend(['shutdown','target-exit','reap'])
            self.assertEqual((_identity,_fd),(identity,99))
            self.assertLessEqual(deadline-stage.time.monotonic(),60)
            stage.save(output/'profile-shutdown.json',dict(forced_target_kill=False))
            return dict(pid=7,returncode=0),[]
        def load(name):
            if name!='pair_work_contract':raise AssertionError(name)
            def work(*_args):calls.append('work');return dict(status='VALID',errors=[])
            return SimpleNamespace(validate_work=work)
        with patch.object(stage.legacy,'require_guard'), \
                patch.object(stage.sync,'Session',return_value=session), \
                patch.object(stage.sync,'make_name',return_value='q4t-suffix-1-owned'), \
                patch.object(stage,'wait_ready',return_value=(identity,99)), \
                patch.object(stage,'require_live'), \
                patch.object(stage.old.http,'event'), \
                patch.object(stage.old.http,'capacity_evidence',return_value={'matches_requested':True}), \
                patch.object(stage.old.admission,'run_cases',side_effect=request), \
                patch.object(stage,'loaded_libraries',return_value={}), \
                patch.object(stage,'cleanup_owned',side_effect=cleanup), \
                patch.object(stage.MeasurementGuard,'arm'), \
                patch.object(stage.MeasurementGuard,'disarm'), \
                patch.object(stage.runner,'pidfd_exited',return_value=True), \
                patch.object(stage.os,'close'), \
                patch.object(stage.old,'audit_execution',return_value=dict(complete=True,errors=[])), \
                patch.object(stage,'module',side_effect=load), \
                patch.object(stage.runner,'export_database',side_effect=lambda *_:calls.append('export')), \
                patch.object(stage,'snapshot_logs',side_effect=lambda *_:calls.append('snapshot')), \
                patch.object(stage,'analyze_capture',side_effect=lambda *_:calls.append('analyze')):
            stage.custom_step(value,step['name'],self.base/'plan.json')
        self.assertEqual(calls,['start','launch','http','report','shutdown','target-exit',
                                'reap','work','export','snapshot','analyze'])
        result=json.loads((output/'exit.json').read_text())
        self.assertTrue(result['recorded_gpu_qualification'])
        self.assertFalse(result['full_trace_integrity'])

    def test_start_failure_keeps_cleanup_errors_and_never_exports(self):
        for mode in ('start','log_limit','deadline','cleanup_failure'):
            with self.subTest(mode=mode):
                base=self.base/mode;value=plan(base)
                Path(value['output']).mkdir(parents=True)
                output=Path(value['output'])/'nsys8-off'
                session=MagicMock();session.name='q4t-suffix-1-2-'+'a'*16+'-owned'
                session.owner=dict(pid=1,start_ticks=2)
                session.output=output;session._closed=False;session.launch_process=None
                session.can_shutdown=True;session.shutdown_attempted=False
                session.shutdown_succeeded=False
                session.reap_adopted.return_value=dict(complete=True,remaining=[],not_children=[])
                def factory(*_args,**kwargs):
                    session.deadline=kwargs['deadline'];session.check=kwargs['check']
                    return session
                def start():
                    if mode=='log_limit':
                        with (output/'nvlog.log').open('wb') as stream:
                            stream.truncate(stage.LOG_LIMIT+1)
                        session.check()
                    if mode=='deadline':raise TimeoutError('measurement timeout')
                    raise RuntimeError('start failed')
                session.start.side_effect=start
                def shutdown(timeout):
                    self.assertLessEqual(timeout,15)
                    session.check()  # The log cap/expired measurement does not run here.
                    session.shutdown_attempted=True
                    if mode=='cleanup_failure':raise RuntimeError('owned shutdown failed')
                    session.shutdown_succeeded=True
                    return dict(returncode=0)
                session.shutdown.side_effect=shutdown
                session.close.side_effect=lambda:setattr(session,'_closed',True)
                expected=RuntimeError if mode=='start' else stage.legacy.EnvironmentAbort
                with patch.object(stage.legacy,'require_guard'), \
                        patch.object(stage.sync,'Session',side_effect=factory), \
                        patch.object(stage.sync,'make_name',return_value=session.name), \
                        patch.object(stage.old.http,'event'), \
                        patch.object(stage.MeasurementGuard,'arm'), \
                        patch.object(stage.MeasurementGuard,'disarm'), \
                        patch.object(stage.runner,'export_database') as export, \
                        patch.object(stage,'analyze_capture') as analysis, \
                        patch.object(stage.runner,'stop_profile_target') as inherited:
                    with self.assertRaises(expected):
                        stage.custom_step(value,'nsys8-off',base/'plan.json')
                export.assert_not_called();analysis.assert_not_called();inherited.assert_not_called()
                session.launch.assert_not_called();session.await_report.assert_not_called()
                session.shutdown.assert_called_once();session.close.assert_called_once()
                row=json.loads((output/'exit.json').read_text())
                self.assertFalse(row['http_output_checks_passed'])
                self.assertFalse(row['trace_exports_complete'])
                self.assertFalse(row['measurement_failure']['repaired_by_cleanup'])
                self.assertEqual(bool(row['owned_cleanup_errors']),mode=='cleanup_failure')
                self.assertEqual(row['environment_abort'],mode!='start')
                self.assertEqual(bool(row['cleanup_errors']),mode!='start')
                budget=json.loads((output/'cleanup-budget.json').read_text())
                self.assertFalse(budget['measurement_check_used'])
                self.assertLessEqual(budget['deadline_monotonic']-budget['started_monotonic'],60)

    def test_install_reuses_monitor_barriers_and_replaces_only_stage_adapters(self):
        work=SimpleNamespace(audit_server=object())
        with patch.object(stage,'module',return_value=work), \
                patch.object(stage.legacy,'clean_env'), \
                patch.object(stage.legacy,'stop_group'), \
                patch.object(stage.legacy,'require_monitor'), \
                patch.object(stage.old.telemetry,'sample'), \
                patch.object(stage.old,'validate'),patch.object(stage.old,'custom_step'), \
                patch.object(stage.old,'__file__'),patch.object(stage.old.admission,'audit_server'):
            stage.install()
            self.assertIs(stage.old.telemetry.sample,stage.runner.sample_with_failure_context)
            self.assertIs(stage.legacy.stop_group,stage.runner.stop_group_with_final_flush)
            self.assertIs(stage.legacy.require_monitor,stage.runner.require_monitor_with_pending)
            self.assertIs(stage.old.admission.audit_server,work.audit_server)
            self.assertIs(stage.old.custom_step,stage.custom_step)


if __name__=='__main__':
    unittest.main()
