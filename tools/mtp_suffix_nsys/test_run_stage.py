"""Pure CPU contracts for role binding and named-session control; no profilers."""
import copy
import importlib.util
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


def plan():
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
    return dict(http_requests=6, http_loads=4, stage_timeout_seconds=5400,
        direct_contract_timeout_seconds=1200, fixture_variants=copy.deepcopy(runner.VARIANTS),
        fixture_timeout_seconds=300, fixture_nvlog_limit_bytes=64*1024**2,
        fixture_contract=copy.deepcopy(runner.FIXTURE_CONTRACT),
        nsys=dict(profile_options=list(runner.OPTIONS), path='/fixture-only/nsys'),
        retries=0, warmups=0, observation_build_limit=0, runtime_optimization_candidates=0,
        steps=steps)


class RunnerContracts(unittest.TestCase):
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
            process = MagicMock()
            process.poll.return_value = 0
            process.returncode = 0
            with patch.object(runner.subprocess, 'Popen', return_value=process) as spawn:
                runner.session_stop(plan(), output, 'q4t-suffix-100-test')
                self.assertEqual(spawn.call_args.args[0],
                    ['/fixture-only/nsys', 'stop', '--session=q4t-suffix-100-test'])
                self.assertEqual(json.loads((output/'session-stop-exit.json').read_text())['returncode'], 0)
            with patch.object(runner.subprocess, 'Popen') as spawn:
                with self.assertRaises(ValueError):
                    runner.session_stop(plan(), output, 'other-session')
                spawn.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            process = MagicMock()
            process.poll.return_value = None
            process.returncode = -15
            with patch.object(runner.subprocess, 'Popen', return_value=process):
                with self.assertRaisesRegex(RuntimeError, 'cap failure'):
                    runner.session_stop(plan(), output, 'q4t-suffix-100-test',
                        check=lambda: (_ for _ in ()).throw(RuntimeError('cap failure')))
            process.terminate.assert_called_once()
            self.assertIn('cap failure', json.loads((output/'session-stop-exit.json').read_text())['failure'])

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
            (output/'target-identity.json').write_text(json.dumps(dict(pid=10)))
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


if __name__ == '__main__':
    unittest.main()
