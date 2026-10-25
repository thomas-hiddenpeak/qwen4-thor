"""Pure CPU contracts for role binding and named-session control; no profilers."""
import copy
import importlib.util
import itertools
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location('nsys_runner_under_test',
                                             Path(__file__).with_name('run_stage.py'))
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def plan(controlled=False):
    first = dict(name='fixture-controls', kind='contract', measurement_role='collector_contract',
                 http_requests=0, http_loads=0, timeout=1200, execution_diagnostic=False, cases=[])
    steps = [first]
    for name, suffix, tokens, count in runner.MODEL_STEPS:
        cases = [dict(id='context-8192', input_tokens=8170, repeats=1,
                      output_tokens=256, role='development_diagnostic')]
        if count == 2:
            cases.append(dict(id='context-204800', input_tokens=204781, repeats=1,
                              output_tokens=256, role='development_diagnostic'))
        steps.append(dict(name=name, head='subset', suffix=suffix,
            measurement_role='nsys_profile', execution_diagnostic=True,
            profile_input_tokens=tokens, kind='timing', http_requests=count,
            http_loads=1, timeout=900, cases=cases))
    value = dict(http_requests=6, http_loads=4, stage_timeout_seconds=5400,
        direct_contract_timeout_seconds=1200, fixture_variants=copy.deepcopy(runner.VARIANTS),
        fixture_timeout_seconds=300, fixture_nvlog_limit_bytes=64*1024**2,
        fixture_contract=copy.deepcopy(runner.FIXTURE_CONTRACT),
        nsys=dict(profile_options=list(runner.OPTIONS), path='/fixture-only/nsys'),
        retries=0, warmups=0, observation_build_limit=0, runtime_optimization_candidates=0,
        steps=steps)
    if controlled:
        value.update(stage_timeout_seconds=4800, direct_contract_timeout_seconds=600,
            fixture_variants=copy.deepcopy(runner.INTERACTIVE_VARIANTS),
            fixture_reuse={'direct-stop': {}, 'wrapper-stop': {}},
            fixture_new_captures=2, fixture_reused_local_controls=2,
            fixture_candidate='wrapper-interactive')
        value['steps'][0]['timeout'] = 600
        value['nsys']['interactive_contract'] = runner.interactive.contract()
    return value


class RunnerContracts(unittest.TestCase):
    def test_fixture_interactive_stop_precedes_release_and_session_cleanup(self):
        for automatic_mode, stop_fails in itertools.product((False, True), repeat=2):
            with self.subTest(automatic=automatic_mode, stop_fails=stop_fails), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory)
                value = plan(True)
                if automatic_mode:
                    value['nsys']['interactive_contract'] = runner.automatic.contract()
                value.update(fixture_binary='/fake/fixture', fixture_log_config='log={log}\n')
                variant = value['fixture_variants'][0]
                output = parent/variant['name']
                session, launcher = MagicMock(), MagicMock()
                session.name, session.output = 'q4t-suffix-100-test', output
                session.owner = dict(pid=100,start_ticks=1)
                session.can_shutdown, session.shutdown_attempted = True, False
                launcher.pid, launcher.returncode = 200, 0 if automatic_mode else None
                calls = []
                target_checks = [0]
                session.start.side_effect = lambda: calls.append('start')
                def launch(target, stream, envvars=None):
                    calls.append('launch')
                    self.assertEqual(target[0], '/fake/fixture')
                    self.assertEqual(envvars['NVLOG_CONFIG_FILE'], str(output/'nvlog.config'))
                    stream.write('[fixture-config] ' + json.dumps(dict(pid=10)) + '\n')
                    stream.write('[fixture-ready] ' + json.dumps(dict(pid=10,passed=True,
                        completed_steps=32,profiler_start_error=0,profiler_stop_error=0,
                        final_sync_error=0)) + '\n')
                    stream.flush()
                    return launcher
                session.launch.side_effect = launch
                def poll():
                    if not (output/'release').exists():
                        return launcher.returncode
                    if not (output/'mock-target-exit').exists():
                        (output/'mock-target-exit').touch()
                        with (output/'server.log').open('a') as target_log:
                            target_log.write('[fixture-exit] ' + json.dumps(dict(
                                pid=10,released=True,returncode=0)) + '\n')
                        launcher.returncode = 0
                    return 0
                launcher.poll.side_effect = poll
                def stop(**_kwargs):
                    calls.append('report-ready' if automatic_mode else 'stop')
                    self.assertFalse((output/'release').exists())
                    if stop_fails:
                        raise RuntimeError('control stop rejected')
                session.stop.side_effect = stop
                session.await_report.side_effect = stop
                def shutdown():
                    self.assertEqual((output/'release').exists(), not automatic_mode or stop_fails)
                    calls.append('shutdown')
                    session.shutdown_attempted = True
                session.shutdown.side_effect = shutdown
                def target_exited(*_args):
                    if not (output/'release').exists():
                        return False
                    target_checks[0] += 1
                    return target_checks[0] >= 2
                session.reap_adopted.return_value = dict(reaped=[],remaining=[],not_children=[],vanished=[])
                session.close.side_effect = lambda: calls.append('close')
                def export(*_args):
                    path = output/'trace.sqlite'
                    path.write_bytes(b'fake exported database')
                    return path
                with patch.object(runner.automatic if automatic_mode else runner.interactive, 'Session', return_value=session), \
                        patch.object(runner, 'session_name', return_value=session.name), \
                        patch.object(runner, 'pin_fixture', return_value=(dict(pid=10,wrapper_pid=None),123)) as pin, \
                        patch.object(runner.old, 'proc_identity', return_value=dict(pid=200,start_ticks=1)), \
                        patch.object(runner, 'pidfd_exited', side_effect=target_exited), \
                        patch.object(runner.os, 'close'), \
                        patch.object(runner.signal, 'pidfd_send_signal'), \
                        patch.object(runner, 'export_database', side_effect=export), \
                        patch.object(runner, 'nvlog_evidence', return_value=dict(
                            explicit_loss_findings=[],terminal_evidence_present=True)), \
                        patch.object(runner.fixture_audit, 'audit', return_value=dict(
                            fixture_contract_passed=True,gpu_memory_coverage_complete=True,memory_coverage={})), \
                        patch.object(runner.fixture_audit, 'negative_copies', return_value=dict(derivatives={
                            'drop-launch-kernel-pair':dict(expected_rejected=True),
                            'drop-last-nvtx':dict(expected_rejected=True)})), \
                        patch.object(runner.fixture_audit, 'connect') as connect, \
                        patch.object(runner, 'role_diagnostics', return_value=dict(valid=True)), \
                        patch.object(runner.subprocess, 'Popen') as forbidden:
                    connect.return_value.__enter__.return_value.execute.return_value = []
                    if stop_fails:
                        with self.assertRaisesRegex(RuntimeError, 'control stop rejected'):
                            runner.fixture_capture(value,variant,parent/'plan.json',parent,float('inf'))
                    else:
                        result = runner.fixture_capture(value,variant,parent/'plan.json',parent,float('inf'))
                        self.assertTrue(result['known_work_complete'])
                    forbidden.assert_not_called()
                    self.assertIs(pin.call_args.kwargs['session'], session)
                self.assertEqual(calls,['start','launch',
                    'report-ready' if automatic_mode else 'stop','shutdown','close'])
                if automatic_mode:
                    session.stop.assert_not_called()
                self.assertFalse((output/'profile-command.json').exists())
                receipt = json.loads((output/'exit.json').read_text())
                self.assertEqual(receipt['complete'],not stop_fails)
                self.assertEqual(receipt['forced_termination'],stop_fails)

    def test_interactive_target_binds_stable_owner_and_exact_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            actual = dict(pid=10, start_ticks=50, ppid=20, pgid=100, state='S')
            wrapper = dict(pid=20, start_ticks=60, ppid=100, pgid=100, state='S')
            owner = dict(pid=100, start_ticks=1)
            session = MagicMock()
            session.prove_descendant.side_effect = lambda pid: dict(
                owner=dict(owner), target=dict(actual if pid == 10 else wrapper),
                chain=copy.deepcopy([actual, wrapper, owner]))
            declared = dict(actual, wrapper_pid=20, wrapper_start_ticks=60)
            (output/'fixture-target-identity.json').write_text(json.dumps(declared))
            (output/'target-identity.json').write_text(json.dumps(declared))
            value = dict(fixture_binary='/proc/10/exe', candidate='/proc/10/exe')
            with patch.object(Path, 'resolve', lambda path, *args, **kwargs: path), \
                    patch.object(runner.old, 'proc_identity', side_effect=lambda _pid: dict(actual)), \
                    patch.object(runner.os, 'pidfd_open', return_value=123), \
                    patch.object(runner.os, 'close') as close:
                result, fd = runner.pin_fixture(value, output, 999, 10, True, session=session)
                self.assertEqual(fd, 123)
                self.assertEqual(result['interactive_ownership']['owner']['pid'], 100)
                close.assert_not_called()
                self.assertEqual(runner.bind_model_target(value, output, session)['pid'], 10)
                bad = dict(actual, ppid=999)
                with patch.object(runner.old, 'proc_identity', return_value=bad):
                    with self.assertRaisesRegex(RuntimeError, 'wrapper'):
                        runner.bind_model_target(value, output, session)
                wrapper['start_ticks'] = 61
                with self.assertRaisesRegex(RuntimeError, 'wrapper identity'):
                    runner.pin_fixture(value, output, 999, 10, True, session=session)
                session.prove_descendant.side_effect = RuntimeError('foreign ancestry')
                with self.assertRaisesRegex(RuntimeError, 'foreign ancestry'):
                    runner.pin_fixture(value, output, 999, 10, False, session=session)

    def test_interactive_cleanup_preserves_failure_and_never_skips_close(self):
        for failure in ('none', 'shutdown', 'remaining', 'reap', 'close', 'not-owned'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                session = MagicMock()
                session.name, session.output = 'q4t-suffix-100-test', Path(directory)
                session.can_shutdown = failure != 'not-owned'
                session.shutdown_attempted = False
                session.shutdown.return_value = {'returncode': 0}
                session.reap_adopted.return_value = dict(reaped=[], remaining=[], not_children=[], vanished=[])
                if failure == 'shutdown':
                    session.shutdown.side_effect = RuntimeError('shutdown failed')
                if failure == 'remaining':
                    session.reap_adopted.return_value['remaining'] = [10]
                if failure == 'reap':
                    session.reap_adopted.side_effect = RuntimeError('reap failed')
                if failure == 'close':
                    session.close.side_effect = RuntimeError('close failed')
                errors = []
                runner.finish_interactive(session, errors)
                session.close.assert_called_once()
                session.reap_adopted.assert_called_once_with(timeout=5)
                if failure == 'not-owned':
                    session.shutdown.assert_not_called()
                else:
                    session.shutdown.assert_called_once()
                self.assertEqual(bool(errors), failure not in ('none', 'not-owned'))
                receipt = json.loads((Path(directory)/'interactive-final-cleanup.json').read_text())
                self.assertEqual(receipt['errors'], errors)

    def test_historical_controls_keep_failed_stage_separate_from_new_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run = root/'old-run'
            source_run.mkdir()
            value = plan(True)
            value['fixture_binary_sha256'] = 'fixture-identity'
            source_plan = root/'old-plan.json'
            source_plan.write_text(json.dumps(dict(output=str(source_run),
                fixture_binary_sha256='fixture-identity', fixture_contract=value['fixture_contract'])))
            seal, refs = {}, {}
            for name in ('direct-stop', 'wrapper-stop'):
                output = source_run/'fixture-controls'/name
                (output/'negative-copies').mkdir(parents=True)
                (output/'trace.sqlite').write_bytes(b'fixture database identity')
                (output/'server.log').write_text('fixture output identity')
                audit = dict(known_work_complete=True, fixture_contract_passed=True,
                    errors=[], expected_pid=10, input_hashes=dict(
                        database=runner.legacy.sha(output/'trace.sqlite'),
                        receipt=runner.legacy.sha(output/'server.log')))
                derivatives = {}
                for negative in ('drop-launch-kernel-pair', 'drop-last-nvtx'):
                    path = output/'negative-copies'/(negative+'.sqlite')
                    path.write_bytes(negative.encode())
                    derivatives[negative] = dict(path=str(path), sha256=runner.legacy.sha(path),
                                                expected_rejected=True)
                runner.save(output/'known-work-audit.json', audit)
                runner.save(output/'negative-controls.json', dict(
                    original_sha256=runner.legacy.sha(output/'trace.sqlite'), derivatives=derivatives))
                runner.save(output/'exit.json', dict(complete=True, cleanup_errors=[],
                    forced_termination=False, failure=None, actual_fixture_pid=10))
                runner.save(output/'profile-diagnostics.json', dict(actual_target_pid=10,
                    valid=False, warnings_require_review=['actual target warning retained']))
                refs[name] = dict(source_plan=str(source_plan),
                    source_plan_sha256=runner.legacy.sha(source_plan), output=str(output),
                    known_work_audit_sha256=runner.legacy.sha(output/'known-work-audit.json'),
                    negative_controls_sha256=runner.legacy.sha(output/'negative-controls.json'),
                    exit_sha256=runner.legacy.sha(output/'exit.json'),
                    profile_diagnostics_sha256=runner.legacy.sha(output/'profile-diagnostics.json'),
                    raw_hashes={n:runner.legacy.sha(output/n) for n in (
                        'trace.sqlite','server.log','known-work-audit.json','profile-diagnostics.json')},
                    source_stage_validity='ENV_ABORT',
                    scope='historical_partial_known_work_only; no new sample or diagnostics pass')
                seal.update({str(p.relative_to(source_run)):runner.legacy.sha(p)
                             for p in output.rglob('*') if p.is_file()})
            runner.save(source_run/'files.json', seal)
            for row in refs.values():
                row.update(source_raw_seal=str(source_run/'files.json'),
                           source_raw_seal_sha256=runner.legacy.sha(source_run/'files.json'))
            value['fixture_reuse'] = refs
            reused = runner.reused_control_evidence(value)
            self.assertTrue(all(not row['new_sample'] and not row['diagnostics_valid']
                                for row in reused['controls'].values()))
            rows = [dict(name=v['name'], known_work_complete=True, negative_controls_passed=True,
                diagnostics_valid=True, terminal_evidence_present=True,
                gpu_memory_coverage_complete=True) for v in runner.INTERACTIVE_VARIANTS]
            self.assertTrue(runner.collector_gate(rows, reused))
            self.assertFalse(runner.collector_gate(rows))
            for key in ('diagnostics_valid', 'gpu_memory_coverage_complete'):
                bad = copy.deepcopy(rows)
                bad[-1][key] = False
                self.assertFalse(runner.collector_gate(bad, reused))
            for key, changed in [('source_stage_validity','VALID'), ('exit_sha256','wrong'),
                                  ('scope','promoted')]:
                bad = copy.deepcopy(value)
                bad['fixture_reuse']['direct-stop'][key] = changed
                with self.subTest(reuse_field=key), self.assertRaises(RuntimeError):
                    runner.reused_control_evidence(bad)

    def test_model_interactive_sequence_and_real_target_exit_are_independent(self):
        for automatic_mode, stop_fails in itertools.product((False, True), repeat=2):
            with self.subTest(automatic=automatic_mode, stop_fails=stop_fails), tempfile.TemporaryDirectory() as directory:
                value = plan(True)
                if automatic_mode:
                    value['nsys']['interactive_contract'] = runner.automatic.contract()
                value.update(output=directory, candidate='/fake/q4t', model='/fake/model',
                             artifact='/fake/subset', port=9999)
                step = value['steps'][1]
                step['cases'][0]['expected_output_sha256'] = 'correct-output'
                output = Path(directory)/step['name']
                calls = []
                session, launcher = MagicMock(), MagicMock()
                session.name = 'q4t-suffix-100-test'
                session.output = output
                session.can_shutdown, session.shutdown_attempted = True, False
                launcher.pid, launcher.returncode = 200, None
                launcher.poll.return_value = None
                session.start.side_effect = lambda: calls.append('start')
                def launch(target, stream, envvars=None):
                    calls.append('launch')
                    self.assertIn('--target-wrapper', target)
                    self.assertEqual(envvars['Q4T_EXEC_PROFILE_INPUT'], '8170')
                    (output/'target-identity.json').write_text(json.dumps(dict(pid=10, start_ticks=50)))
                    stream.write('serving on port 9999\n[exec-capture] event=begin pid=10 error=0\n'
                                 '[exec-capture] event=end pid=10 error=0\n')
                    stream.flush()
                    return launcher
                session.launch.side_effect = launch
                def stop(**_kwargs):
                    calls.append('report-ready' if automatic_mode else 'stop')
                    if stop_fails:
                        raise RuntimeError('interactive stop failed')
                session.stop.side_effect = stop
                session.await_report.side_effect = stop
                def target_stop(*_args):
                    calls.append('target-exit')
                    launcher.returncode = 0
                    launcher.poll.return_value = 0
                    return dict(returncode=0)
                def shutdown():
                    calls.append('shutdown')
                    session.shutdown_attempted = True
                session.shutdown.side_effect = shutdown
                session.reap_adopted.return_value = dict(reaped=[], remaining=[], not_children=[], vanished=[])
                session.close.side_effect = lambda: calls.append('close')
                batch = [dict(outputs=['correct-output'], responses=[dict(actual_output=256, finish=['length'])])]
                with patch.object(runner.legacy, 'require_guard'), \
                        patch.object(runner.automatic if automatic_mode else runner.interactive, 'Session', return_value=session), \
                        patch.object(runner, 'session_name', return_value=session.name), \
                        patch.object(runner, 'bind_model_target', return_value=dict(pid=10,start_ticks=50)), \
                        patch.object(runner.old, 'proc_identity', return_value=dict(pid=10,start_ticks=50,state='S')), \
                        patch.object(runner.old.http, 'event'), \
                        patch.object(runner.old.http, 'capacity_evidence', return_value=dict(matches_requested=True)), \
                        patch.object(runner.old.admission, 'run_cases', return_value=batch), \
                        patch.object(runner.old, 'audit_execution', return_value=dict(complete=True)), \
                        patch.object(runner, '_original_stop_target', side_effect=target_stop), \
                        patch.object(runner, 'export_profile') as export, \
                        patch.object(runner.subprocess, 'Popen') as forbidden:
                    if stop_fails:
                        with self.assertRaisesRegex(RuntimeError, 'diagnostic group failed'):
                            runner.interactive_model_step(value, step['name'], Path(directory)/'plan.json')
                        export.assert_not_called()
                    else:
                        runner.interactive_model_step(value, step['name'], Path(directory)/'plan.json')
                        export.assert_called_once()
                    forbidden.assert_not_called()
                completion = 'report-ready' if automatic_mode else 'stop'
                tail = (['shutdown','target-exit'] if automatic_mode and not stop_fails
                        else ['target-exit','shutdown'])
                self.assertEqual(calls, ['start','launch',completion,*tail,'close'])
                if automatic_mode:
                    session.stop.assert_not_called()
                receipt = json.loads((output/'exit.json').read_text())
                self.assertEqual(receipt['server'], 0)
                self.assertEqual(receipt['http_output_checks_passed'], not stop_fails)
                self.assertEqual(receipt['trace_exports_complete'], not stop_fails)

    def test_pending_barrier_waits_for_complete_ledger(self):
        heartbeat = dict(exit_pending_count=1)
        def finish(_duration):
            heartbeat['exit_pending_count'] = 0
        with patch.object(runner, '_original_require_monitor') as check, \
                patch.object(runner.time, 'monotonic', return_value=10), \
                patch.object(runner.time, 'sleep', side_effect=finish) as sleep:
            runner.require_monitor_with_pending('sampler', [], heartbeat, 5)
        self.assertEqual(check.call_count, 2)
        sleep.assert_called_once_with(.05)
        self.assertEqual(heartbeat['exit_pending_count'], 0)

    def test_pending_barrier_cannot_override_failure_or_deadline(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                with patch.object(runner, '_original_require_monitor',
                        side_effect=runner.legacy.EnvironmentAbort('monitor') if failure else None), \
                        patch.object(runner.time, 'monotonic', side_effect=[10, 16]), \
                        patch.object(runner.time, 'sleep') as sleep:
                    with self.assertRaises(runner.legacy.EnvironmentAbort):
                        runner.require_monitor_with_pending(None, [],
                            dict(exit_pending_count=1), 5)
                    sleep.assert_not_called()

    def test_failure_flush_pending_is_not_complete_even_with_fresh_time(self):
        with tempfile.TemporaryDirectory() as directory:
            stop = MagicMock()
            stop.is_set.return_value = False
            context = dict(output=Path(directory)/'memory.jsonl', failures=[],
                heartbeat=dict(last_sample=11, last_cycle_duration_seconds=.1,
                               exit_pending_count=1), stop=stop, returned=False)
            with patch.object(runner, '_failure_monitor', context), \
                    patch.object(runner, '_original_stop_group'), \
                    patch.object(runner.time, 'monotonic', side_effect=[10, 16]):
                with self.assertRaises(runner.legacy.EnvironmentAbort):
                    runner.stop_group_with_final_flush(MagicMock(pid=100))
            receipt = json.loads((Path(directory)/'failure-monitor-barrier.json').read_text())
            self.assertFalse(receipt['complete'])

    def test_frozen_scope_order_budget(self):
        value = plan()
        runner.validate(value)
        for key, changed in [('http_requests', 7), ('http_loads', 5),
                ('stage_timeout_seconds', 5401), ('direct_contract_timeout_seconds', 1201),
                ('fixture_timeout_seconds', 301), ('fixture_nvlog_limit_bytes', 1), ('retries', 1)]:
            with self.subTest(field=key):
                bad = copy.deepcopy(value)
                bad[key] = changed
                with self.assertRaises(ValueError):
                    runner.validate(bad)
        bad = copy.deepcopy(value)
        bad['fixture_variants'].reverse()
        with self.assertRaises(ValueError):
            runner.validate(bad)
        controlled = plan(True)
        runner.validate(controlled)
        automatic = copy.deepcopy(controlled)
        automatic['nsys']['interactive_contract'] = runner.automatic.contract()
        runner.validate(automatic)
        automatic['nsys']['interactive_contract']['report_timeout_seconds'] = 46
        with self.assertRaises(ValueError):
            runner.validate(automatic)
        for key, changed in [('stage_timeout_seconds', 4801),
                ('direct_contract_timeout_seconds', 601), ('fixture_new_captures', 4),
                ('fixture_reused_local_controls', 0), ('fixture_candidate', 'wrapper-external')]:
            with self.subTest(interactive_field=key):
                bad = copy.deepcopy(controlled)
                bad[key] = changed
                with self.assertRaises(ValueError):
                    runner.validate(bad)
        bad = copy.deepcopy(controlled)
        bad['nsys']['interactive_contract']['start_options'].append('--kill=sigterm')
        with self.assertRaises(ValueError):
            runner.validate(bad)
        bad = copy.deepcopy(value)
        bad['steps'][3]['cases'].reverse()
        with self.assertRaises(ValueError):
            runner.validate(bad)
        rows = [dict(name=v['name'], known_work_complete=True, negative_controls_passed=True,
                     diagnostics_valid=True, terminal_evidence_present=True,
                     gpu_memory_coverage_complete=True) for v in runner.VARIANTS]
        self.assertTrue(runner.collector_gate(rows))
        for key in ('known_work_complete', 'negative_controls_passed', 'diagnostics_valid',
                    'terminal_evidence_present', 'gpu_memory_coverage_complete'):
            with self.subTest(candidate_requirement=key):
                bad = copy.deepcopy(rows)
                bad[-1][key] = False
                self.assertFalse(runner.collector_gate(bad))
        rows[0]['gpu_memory_coverage_complete'] = False
        self.assertTrue(runner.collector_gate(rows))
        self.assertFalse(runner.collector_gate(rows[:-1]))
        bad = copy.deepcopy(value)
        bad['fixture_contract']['memset_bytes_even'] = 0
        with self.assertRaises(ValueError):
            runner.validate(bad)

    def test_no_inherited_instrumentation(self):
        env = dict(PATH='/bin', NVLOG_CONFIG_FILE='bad', NVLOG_CONFIG_RAW='bad',
                   NVLOG_LOG_FILE='bad', NSYS_CONFIG_DIRECTIVES='bad',
                   CUDA_INJECTION64_PATH='bad', NVTX_INJECTION64_PATH='bad',
                   QUADD_CUDA_CONFIG='bad', CUPTI_INJECTION64_PATH='bad')
        with patch.object(runner, '_original_clean_env', return_value=env):
            self.assertEqual(runner.clean_env(), {'PATH': '/bin'})

    def test_capture_receipt_requires_real_pid_and_success(self):
        text = ('[exec-capture] event=begin pid=10 error=0 input_tokens=8170\n'
                '[exec-capture] event=end pid=10 error=0 input_tokens=8170\n')
        self.assertEqual(runner.capture_end_receipt(text, 10)['event'], 'end')
        for bad in [text.replace('pid=10', 'pid=11'), text.replace('error=0', 'error=1'),
                    text.splitlines()[0], text + text]:
            with self.subTest(receipt=bad):
                with self.assertRaises(RuntimeError):
                    runner.capture_end_receipt(bad, 10)

    def test_wrapper_warnings_are_bound_unique_and_empty(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE CUPTI_ACTIVITY_KIND_RUNTIME(globalTid INTEGER)')
            db.execute('CREATE TABLE NVTX_EVENTS(globalTid INTEGER)')
            tables = {'CUPTI_ACTIVITY_KIND_RUNTIME', 'NVTX_EVENTS'}
            def diagnostic(messages):
                return dict(messages=messages, errors=[], warnings_require_review=list(messages),
                    unavailable_auxiliary_features=[], profiler_log_findings=[], valid=not messages)
            def row(text, pid=20, severity='Warning'):
                return dict(globalPid=pid << 24, text=text, severity_name=severity)
            allowed = [row(t) for t in sorted(runner.WRAPPER_WARNINGS)]
            for messages in [[], allowed[:1], allowed]:
                with self.subTest(allowed=len(messages)), patch.object(runner,
                        '_original_diagnostics', return_value=diagnostic(messages)):
                    self.assertTrue(runner.role_diagnostics(db, tables, Path('/unused'), 10, 20)['valid'])
            for messages in [allowed + allowed[:1], [row('unknown warning')],
                    [row(allowed[0]['text'], 10)], [row(allowed[0]['text'], 30)],
                    [row('unknown severity', severity='UnknownLevel')]]:
                with self.subTest(rejected=messages), patch.object(runner,
                        '_original_diagnostics', return_value=diagnostic(messages)):
                    self.assertFalse(runner.role_diagnostics(db, tables, Path('/unused'), 10, 20)['valid'])
            um = row('CUDA device 0: Unified Memory trace is not supported by the current '
                     'driver version or configuration.', 10)
            inherited = diagnostic([um])
            inherited['warnings_require_review'] = []
            inherited['unavailable_auxiliary_features'] = [um]
            with patch.object(runner, '_original_diagnostics', return_value=inherited):
                self.assertFalse(runner.role_diagnostics(db, tables, Path('/unused'), 10, 20)['valid'])
            db.execute('INSERT INTO NVTX_EVENTS VALUES(?)', ((20 << 24) | 21,))
            with patch.object(runner, '_original_diagnostics', return_value=diagnostic(allowed)):
                self.assertFalse(runner.role_diagnostics(db, tables, Path('/unused'), 10, 20)['valid'])

    def test_session_stop_exact_name_and_failure_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            session = MagicMock()
            session.name = 'q4t-suffix-100-test'
            session.output = output
            session.stop.return_value = dict(returncode=0, scope='control only')
            value = dict(plan(True), _interactive_session=session)
            with patch.object(runner.subprocess, 'Popen') as spawn:
                self.assertEqual(runner.session_stop(value, output, session.name),
                                 dict(returncode=0, scope='control only'))
                session.stop.assert_called_once_with(timeout=45, check=None)
                spawn.assert_not_called()
            with patch.object(runner.subprocess, 'Popen') as spawn:
                with self.assertRaises(ValueError):
                    runner.session_stop(value, output, 'other-session')
                spawn.assert_not_called()
            for bad, path, name in [(plan(), output, session.name),
                    (value, output/'wrong', session.name),
                    (value, output, 'q4t-suffix-100-other')]:
                with self.subTest(bound_session=name, output=path):
                    with self.assertRaisesRegex(RuntimeError, 'bound interactive'):
                        runner.session_stop(bad, path, name)
            session.stop.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'cap failure'):
                runner.session_stop(value, output, session.name,
                    check=lambda: (_ for _ in ()).throw(RuntimeError('cap failure')))
            session.stop.assert_not_called()
            session.stop.side_effect = RuntimeError('control failed')
            with self.assertRaisesRegex(RuntimeError, 'control failed'):
                runner.session_stop(value, output, session.name)

    def test_ready_json_and_nvlog_terminal_not_inferred(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'server.log'
            ready = dict(pid=10, passed=True, completed_steps=32,
                profiler_start_error=0, profiler_stop_error=0, final_sync_error=0)
            path.write_text('unrelated fixture-ready word\n')
            self.assertIsNone(runner.fixture_ready(path))
            line = '[fixture-ready] ' + json.dumps(ready) + '\n'
            path.write_text(line)
            self.assertEqual(runner.fixture_ready(path), ready)
            for bad in [line+line, line.replace('true', 'false')]:
                path.write_text(bad)
                with self.assertRaises(RuntimeError):
                    runner.fixture_ready(path)
            log = Path(directory)/'nvlog.log'
            flush = 'I fake| pid=10 tid=10 | Flushing activity records with flag: 0.\n'
            done = 'I fake| pid=10 tid=10 | CUDA: ReportCUDADone: DONE.\n'
            for text, terminal in [(flush+done, True), (flush, False), (done, False)]:
                log.write_text(text)
                result = runner.nvlog_evidence(log, 10)
                self.assertEqual(result['terminal_evidence_present'], terminal)
                self.assertIsNone(result['dropped_count'])
            log.write_text(flush+done+'W fake| pid=10 tid=10 | CUPTI dropped 3 records.\n')
            self.assertEqual(len(runner.nvlog_evidence(log, 10)['explicit_loss_findings']), 1)
            log.write_text(flush.replace('pid=10', 'pid=11'))
            with self.assertRaises(RuntimeError):
                runner.nvlog_evidence(log, 10)

    def test_failed_step_requires_fresh_monitor_after_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            heartbeat = dict(last_sample=11, last_cycle_duration_seconds=.2)
            stop = MagicMock()
            stop.is_set.return_value = False
            context = dict(output=output/'unified-memory.jsonl', failures=[],
                           heartbeat=heartbeat, stop=stop, returned=False)
            child = MagicMock(pid=100)
            with patch.object(runner, '_failure_monitor', context), \
                    patch.object(runner, '_original_stop_group') as cleanup, \
                    patch.object(runner.time, 'monotonic', return_value=10):
                runner.stop_group_with_final_flush(child)
                cleanup.assert_called_once_with(child)
            receipt = json.loads((output/'failure-monitor-barrier.json').read_text())
            self.assertTrue(receipt['complete'])
            self.assertGreaterEqual(receipt['sample_started_monotonic'], receipt['boundary_monotonic'])
            for change in ('failure', 'stale', 'returned'):
                with self.subTest(change=change):
                    current = dict(context, failures=['failure'] if change == 'failure' else [],
                        returned=change == 'returned', heartbeat=dict(last_sample=9,
                            last_cycle_duration_seconds=.1))
                    with patch.object(runner, '_failure_monitor', current), \
                            patch.object(runner, '_original_stop_group'), \
                            patch.object(runner.time, 'monotonic', side_effect=[10, 16]):
                        with self.assertRaises(runner.legacy.EnvironmentAbort):
                            runner.stop_group_with_final_flush(child)
                    receipt = json.loads((output/'failure-monitor-barrier.json').read_text())
                    self.assertFalse(receipt['complete'])
                    self.assertIn('failure', receipt)

    def test_capture_end_waits_boundedly_and_always_cleans_target(self):
        poller = MagicMock()
        flags = runner.select
        normal = getattr(flags, 'POLLRDNORM', 0)
        for events, expected in [([], False), ([(7, flags.POLLIN)], True),
                ([(7, flags.POLLHUP)], True), ([(7, flags.POLLIN | flags.POLLHUP)], True),
                ([(7, flags.POLLIN | normal)], True)]:
            with self.subTest(pidfd_events=events), \
                    patch.object(runner.select, 'poll', return_value=poller):
                poller.poll.return_value = events
                self.assertEqual(runner.pidfd_exited(7, 50), expected)
                poller.poll.assert_called_with(50)
        for events in [[(7, flags.POLLERR)], [(7, flags.POLLNVAL)],
                [(7, flags.POLLIN | flags.POLLERR)], [(7, 1 << 30)],
                [(7, 0)], [(7, normal)], [(8, flags.POLLIN)],
                [(7, flags.POLLIN), (7, flags.POLLHUP)]]:
            with self.subTest(invalid_pidfd_events=events), \
                    patch.object(runner.select, 'poll', return_value=poller):
                poller.poll.return_value = events
                with self.assertRaisesRegex(RuntimeError, 'invalid owned pidfd'):
                    runner.pidfd_exited(7, 50)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output/'target-identity.json').write_text(json.dumps(dict(pid=10, start_ticks=50)))
            begin = '[exec-capture] event=begin pid=10 error=0 input_tokens=8170\n'
            end = '[exec-capture] event=end pid=10 error=0 input_tokens=8170\n'
            log = output/'server.log'
            log.write_text(begin)
            launcher = MagicMock()
            launcher.poll.return_value = None
            local = dict(_active_session='q4t-suffix-100-test')
            with patch.object(runner.time, 'monotonic', side_effect=[0, 1, 2]), \
                    patch.object(runner.time, 'sleep', side_effect=lambda _: log.write_text(begin+end)), \
                    patch.object(runner, 'session_stop') as stop, \
                    patch.object(runner, '_original_stop_target', return_value={'returncode': 0}) as cleanup:
                self.assertEqual(runner.stop_profile_target(local, output, launcher), {'returncode': 0})
                stop.assert_called_once()
                cleanup.assert_called_once()
            log.write_text(begin)
            with patch.object(runner.time, 'monotonic', side_effect=[0, 6]), \
                    patch.object(runner, 'session_stop') as stop, \
                    patch.object(runner, '_original_stop_target') as cleanup:
                with self.assertRaisesRegex(RuntimeError, 'did not arrive'):
                    runner.stop_profile_target(local, output, launcher)
                stop.assert_not_called()
                cleanup.assert_called_once()
            # Startup or ancestry failure cannot authorize the inherited
            # numeric-identity-file -> pidfd signalling path.
            local['_interactive_session'] = MagicMock()
            for startup_failed in (True, False):
                log.write_text(begin if startup_failed else begin+end)
                launcher.poll.return_value = 1 if startup_failed else None
                with self.subTest(startup_failed=startup_failed), \
                        patch.object(runner.time, 'monotonic', return_value=0), \
                        patch.object(runner, 'session_stop') as stop, \
                        patch.object(runner, 'bind_model_target',
                                     side_effect=RuntimeError('unproved owner')) as bind, \
                        patch.object(runner, '_original_stop_target') as cleanup:
                    with self.assertRaisesRegex(RuntimeError, 'cleanup ownership unproved'):
                        runner.stop_profile_target(local, output, launcher)
                    bind.assert_called_once_with(local, output,
                        local['_interactive_session'], cleanup=True)
                    cleanup.assert_not_called()
                    self.assertEqual(stop.call_count, 0 if startup_failed else 1)
                receipt = json.loads((output/'target-cleanup-authorization.json').read_text())
                self.assertFalse(receipt['authorized'])


if __name__ == '__main__':
    unittest.main()
