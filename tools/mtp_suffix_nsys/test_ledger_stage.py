"""CPU-only single-request, ownership and standalone injection contracts."""
import copy
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location('ledger_stage_test',
    Path(__file__).with_name('ledger_stage.py'))
stage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage)


def plan(base):
    fixture = dict(name='fixture-ledger',kind='contract',measurement_role='ledger_fixture',
        http_loads=0,http_requests=0,timeout=120,execution_diagnostic=False,cases=[])
    case = dict(id='context-8192',path=str(base/'input'),input_tokens=8170,
        input_min=8144,input_max=8192,output_tokens=256,repeats=1,
        role='development_diagnostic',prompt_sha256=stage.PROMPT_SHA,
        expected_output_sha256=stage.OUTPUT_SHA)
    model = dict(name='model8-off',kind='timing',measurement_role='cupti_parameters',
        head='subset',suffix=False,execution_diagnostic=True,profile_input_tokens=8170,
        http_loads=1,http_requests=1,timeout=600,cases=[case])
    for step in (fixture,model):
        step['command'] = ['/usr/bin/python3','-B',str(Path(stage.__file__).resolve()),
                          '--plan',str(base/'plan.json'),'--custom-step',step['name']]
    value = dict(http_requests=1,http_loads=1,stage_timeout_seconds=900,
        direct_contract_timeout_seconds=120,retries=0,warmups=0,nsys_processes=0,
        resource_sample_max_age_seconds=5,expected_work=dict(t4=74,t1=3),
        monitor_fix=dict(exit_pending=dict(ttl_seconds=5,capacity=16,poll_timeout_ms=0)),
        candidate=str(base/'q4t'),candidate_sha256=stage.CANDIDATE_SHA,
        fixture_binary=str(base/'probe'),fixture_binary_sha256=stage.FIXTURE_SHA,
        ledger_library=str(base/'ledger.so'),ledger_library_sha256='ledger',
        model=str(base/'model'),artifact=str(base/'artifact'),output=str(base/'run'),
        port=8758,steps=[fixture,model])
    value['files'] = {value['candidate']:stage.CANDIDATE_SHA,
        value['fixture_binary']:stage.FIXTURE_SHA,value['ledger_library']:'ledger',
        case['path']:stage.DATA_SHA}
    return value


def ready(pid=7):
    return dict(pid=pid,mode='nsys',passed=True,gemm_calls=2,controls=2,
        M=4,N=48,K=2560,workspace_bytes=33554432,
        profiler_start_error=0,profiler_stop_error=0,final_sync_error=0)


def fixture_log(path):
    lines = ['[mechanism-ready] '+json.dumps(ready())]
    for phase in ('control_zero','control_nonzero','gemm_first','gemm_cached'):
        words = ([165 if phase=='control_zero' else 90]*512 if phase.startswith('control')
            else [struct.unpack('<I',struct.pack('<f',(n%7-3)*(m%5+1)*(m+1)))[0]>>16
                  for m in range(4) for n in range(48)])
        lines.append('[mechanism-event] '+json.dumps(dict(event='check',phase=phase,
            actual_words=words,expected_words=words,passed=True,mismatches=0)))
    path.write_text('\n'.join(lines)+'\n')


class LedgerStageContracts(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)

    def test_fixed_plan_rejects_extra_work_and_unbound_inputs(self):
        value = plan(self.base)
        stage.validate(value)
        for field in ('http_requests','http_loads','warmups','nsys_processes'):
            with self.subTest(field=field):
                changed = copy.deepcopy(value); changed[field] += 1
                with self.assertRaises(ValueError): stage.validate(changed)
        for mode in ('suffix','case','injected_child_env','library'):
            with self.subTest(mode=mode):
                changed = copy.deepcopy(value)
                if mode=='suffix': changed['steps'][1]['suffix'] = True
                elif mode=='case': changed['steps'][1]['cases'][0]['input_tokens'] = 8192
                elif mode=='injected_child_env': changed['steps'][0]['env'] = {'LD_PRELOAD':'other'}
                else: changed['ledger_library_sha256'] = 'unbound'
                with self.assertRaises(ValueError): stage.validate(changed)

    def test_preload_and_capture_flags_only_enter_target_environment(self):
        value = plan(self.base)
        dirty = dict(PATH='/bin',LD_PRELOAD='foreign',Q4T_EXEC_PROFILE_INPUT='3',
                     CUPTI_INJECTION64_PATH='foreign')
        with patch.object(stage.runner,'clean_env',return_value=dirty):
            client = stage.clean_env()
            model = stage.target_environment(value,self.base,True)
            fixture = stage.target_environment(value,self.base,False)
        self.assertEqual(client,{'PATH':'/bin'})
        self.assertEqual(model['LD_PRELOAD'],value['ledger_library'])
        self.assertEqual(model['Q4T_EXEC_PROFILE_INPUT'],'8170')
        self.assertEqual(model['Q4T_EXEC_NVTX'],'1')
        self.assertNotIn('Q4T_EXEC_PROFILE_INPUT',fixture)
        self.assertEqual(fixture['Q4T_MEMSET_LEDGER_PATH'],str(self.base/'ledger.jsonl'))

    def test_ledger_boundary_requires_complete_successful_summary(self):
        path = self.base/'ledger.jsonl'
        good = json.dumps(dict(event='summary',completed=True))+'\n'
        path.write_text(good)
        result = stage.ledger_boundary(path,7)
        self.assertEqual(result['sha256'],stage.sha(path))
        for value in ('',good.rstrip(),json.dumps(dict(event='summary',completed=False))+'\n',
                      json.dumps(dict(event='api',completed=True))+'\n'):
            with self.subTest(value=value):
                path.write_text(value)
                with self.assertRaises(RuntimeError): stage.ledger_boundary(path,7)
        path.write_text(good); link = self.base/'link'; link.symlink_to(path)
        with self.assertRaises(RuntimeError): stage.ledger_boundary(link,7)

    def test_held_fixture_checks_actual_values_and_real_ready(self):
        log = self.base/'server.log'; fixture_log(log)
        self.assertEqual(stage.held_fixture(log,7)['controls'],2)
        with self.assertRaises(RuntimeError): stage.held_fixture(log,8)
        raw = log.read_text(); log.write_text(raw.replace('"actual_words": [165',
                                                        '"actual_words": [164',1))
        with self.assertRaisesRegex(RuntimeError,'exact output'): stage.held_fixture(log,7)

    def test_fixture_gate_binds_same_plan_raw_and_cleanup(self):
        value = plan(self.base); output = Path(value['output'])/'fixture-ledger'
        output.mkdir(parents=True); planpath = self.base/'plan.json'; planpath.write_text('{}')
        for name in ('ledger-analysis.json','fixture-validation.json'):
            stage.save(output/name,dict(status='VALID',errors=[],completed=True))
        stage.save(output/'cleanup.json',dict(status='NORMAL',forced=False,errors=[]))
        inputs = {str(p):stage.sha(p) for p in output.iterdir()}
        stage.save(output/'gate.json',dict(passed=True,plan_sha256=stage.sha(planpath),
            normal_target_exit=True,inputs=inputs))
        self.assertTrue(stage.fixture_gate(value,planpath)['passed'])
        (output/'ledger-analysis.json').write_text('{}')
        with self.assertRaises(RuntimeError): stage.fixture_gate(value,planpath)

    def test_host_parser_view_preserves_public_non_nsight_role(self):
        step = plan(self.base)['steps'][1]
        def audited(output,view,pid):
            self.assertEqual(view['measurement_role'],'nsys_profile')
            self.assertEqual(pid,7)
            stage.save(output/'execution-audit.json',dict(complete=True,errors=[]))
            return dict(complete=True,errors=[])
        with patch.object(stage.old,'audit_execution',side_effect=audited):
            self.assertTrue(stage.host_audit(self.base,step,7)['complete'])
        self.assertEqual(step['measurement_role'],'cupti_parameters')
        receipt = json.loads((self.base/'host-capture-contract.json').read_text())
        self.assertEqual(receipt['nsys_processes'],0)

    def test_request_lifecycle_ignores_capture_end_but_not_failed_release(self):
        log = self.base/'server.log'
        raw = ('[q4t][request] id=r event=admitted\n'
               '[exec-capture] event=end pid=7 input_tokens=8170 error=0\n'
               '[q4t][request] id=r event=end released=1 outcome=success reason=completed\n')
        log.write_text(raw)
        with patch.object(stage,'BASE_SERVER_AUDIT'):
            stage.audit_server(self.base,1)
            log.write_text(raw.replace('released=1','released=0'))
            with self.assertRaisesRegex(RuntimeError,'lifecycle'): stage.audit_server(self.base,1)

    def test_wrong_executable_binding_closes_pidfd_without_signaling(self):
        target = stage.OwnedTarget(self.base,self.base/'q4t')
        child = MagicMock(pid=7)
        identity = dict(pid=7,start_ticks=1,ppid=123,pgid=123,state='R')
        with patch.object(stage.subprocess,'Popen',return_value=child), \
                patch.object(stage.os,'pidfd_open',return_value=99), \
                patch.object(stage.os,'getpid',return_value=123), \
                patch.object(stage.old,'proc_identity',return_value=identity), \
                patch.object(stage.runner,'pidfd_exited',return_value=False), \
                patch.object(Path,'resolve',return_value=self.base/'other'), \
                patch.object(stage.os,'close') as closed, \
                patch.object(stage.signal,'pidfd_send_signal') as sent:
            with self.assertRaisesRegex(RuntimeError,'binding failed'):
                target.start(['/fake'],{},MagicMock())
        closed.assert_called_once_with(99); sent.assert_not_called()
        self.assertIsNone(target.fd)

    def test_owned_cleanup_marks_forced_or_unknown_exit_as_environment_abort(self):
        for mode in ('normal','kill','unknown'):
            with self.subTest(mode=mode):
                output = self.base/mode; output.mkdir()
                target = stage.OwnedTarget(output,output/'q4t')
                target.child = MagicMock(pid=7,returncode=0)
                target.child.poll.return_value = None
                target.child.wait.side_effect = ([stage.subprocess.TimeoutExpired('q4t',.1),0]
                                                 if mode=='kill' else None)
                target.fd, target.identity = 99,dict(pid=7,start_ticks=1)
                with patch.object(stage.signal,'pidfd_send_signal') as sent, \
                        patch.object(stage.os,'close'), \
                        patch.object(stage.runner,'pidfd_exited',return_value=mode!='unknown'):
                    if mode=='normal': target.close(True,stage.time.monotonic()+30)
                    else:
                        with self.assertRaises(stage.legacy.EnvironmentAbort):
                            target.close(True,stage.time.monotonic()+30)
                result = json.loads((output/'cleanup.json').read_text())
                self.assertEqual(result['status'],'NORMAL' if mode=='normal' else 'ENV_ABORT')
                self.assertTrue(target.closed)
                if mode=='kill': self.assertTrue(result['forced']); self.assertEqual(sent.call_count,2)

    def test_fixture_releases_before_single_analysis_and_gate_after_normal_exit(self):
        value = plan(self.base); Path(value['output']).mkdir()
        step = value['steps'][0]; output = Path(value['output'])/step['name']
        planpath = self.base/'plan.json'; planpath.write_text('{}')
        calls = []
        class Target:
            def __init__(self,_output,_executable):
                self.child = SimpleNamespace(pid=7,returncode=0,poll=lambda:0)
                self.closed = False
            def start(self,_command,env,stream):
                self_env = env
                calls.append('start')
                stream.write('[mechanism-ready] '+json.dumps(ready())+'\n');stream.flush()
                (output/'ledger.jsonl').write_text(json.dumps(dict(event='summary',completed=True))+'\n')
                if self_env.get('LD_PRELOAD') != value['ledger_library']:
                    raise AssertionError('target preload missing')
            def require_live(self,_deadline): pass
            def close(self,normal,_deadline):
                if normal or not (output/'release').exists(): raise AssertionError('release ordering')
                calls.append('exit');self.closed=True
                stage.save(output/'cleanup.json',dict(status='NORMAL',forced=False,errors=[]))
        analyzer = MagicMock()
        def analyze(*_args,**_kwargs):
            self.assertEqual(calls,['start','exit']);calls.append('analyze')
            return dict(status='VALID',errors=[],completed=True)
        analyzer.analyze.side_effect=analyze
        analyzer.validate_fixture.return_value=dict(status='VALID',errors=[],completed=True)
        with patch.object(stage,'OwnedTarget',Target), \
                patch.object(stage,'held_fixture',return_value=ready()), \
                patch.object(stage,'loaded_libraries',return_value={}), \
                patch.object(stage,'analyze_module',return_value=analyzer):
            stage.fixture_step(value,step,planpath)
        analyzer.analyze.assert_called_once()
        self.assertTrue(json.loads((output/'gate.json').read_text())['passed'])
        self.assertEqual(calls,['start','exit','analyze'])


if __name__ == '__main__':
    unittest.main()
