"""CPU-only substitute contracts for the bounded production runner."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('delivery_run_contract', HERE / 'run.py')
run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run)


def summary_rows():
    row = dict(schema=1, status='PASS', count=2, device_bytes=96, allocated=2,
               consumed=0, freed=0, unhanded_remaining=2, handed_live=0,
               release_unknown=0, error='none', backend_rc=0, error_index=0,
               prepared=True, startup_complete=False, finalized=False,
               active=True, strategy='stable_descending_bytes', plan_sha256='a' * 64)
    prepared = dict(row, event='prepared')
    complete = dict(row, event='startup_complete', consumed=2,
                    unhanded_remaining=0, handed_live=2, startup_complete=True)
    final = dict(complete, event='finalized', freed=2, handed_live=0,
                 active=False, finalized=True)
    return [prepared, complete, final]


def log_for(stage, final=False):
    cfg = run.configuration(stage)
    default = cfg['real_default']
    cap = ('[q4t][capacity] requested_max_len={max_len} requested_max_seq={max_seq} '
           'requested_max_prefill={max_prefill} effective_max_len={effective_len} '
           'effective_max_seq={max_seq} effective_max_prefill={effective_prefill} '
           'budget_enabled=1 budget_feasible=true\n').format(
               **cfg, effective_len=131072 if default else cfg['max_len'],
               effective_prefill=2048 if default else cfg['max_prefill'])
    cap += ('[q4t][capabilities] effective mtp={} media_allowed=0 '
            'vision_loaded=0 max_seq={}\n').format(int(cfg['mtp']), cfg['max_seq'])
    cap += ('MTP loaded (k=3 max_seq={})\n'.format(cfg['max_seq']) if cfg['mtp']
            else 'MTP disabled; plain decode\n')
    active = cfg['startup_plan'] != 'off'
    cap += f'[q4t][startup-plan] mode={cfg["startup_plan"]} active={int(active)}\n'
    if active:
        cap += ''.join('[startup-reservation] ' + json.dumps(row) + '\n'
                       for row in summary_rows()[:2])
    cap += '[q4t] serving on port 8850\n'
    if active and final:
        cap += '[startup-reservation] ' + json.dumps(summary_rows()[-1]) + '\n'
    return cap


class Contracts(unittest.TestCase):
    output = None

    def setUp(self):
        self.work = self.output / self._testMethodName
        self.work.mkdir()

    def test_matrix_budget(self):
        self.assertEqual(len(run.STAGES), 6)
        self.assertEqual(sum(run.configuration(s)['requests'] for s in run.STAGES), 9)
        self.assertEqual(sum(run.configuration(s)['cache'] == 'full_hot'
                             for s in run.STAGES), 1)

    def test_true_default_omits_capacity_and_environment(self):
        for stage in ('default-cold-v1', 'default-inherited-v1', 'off-v1',
                      'deploy-mtp-v1', 'deploy-plain-v1'):
            argv = run.server_command(stage, self.work)
            for option in ('--max-len', '--max-prefill', '--max-seq',
                           '--moe-resident-slots', '--mtp', '--mtp-k'):
                self.assertNotIn(option, argv)
            env = run.environment(stage, self.work,
                                  {'Q4T_BAD': '1', 'LD_PRELOAD': 'x', 'HOME': '/tmp'})
            self.assertEqual(env, {'HOME': '/tmp'})
            self.assertIsNone(run.configuration(stage)['memory_max'])
        self.assertEqual(run.server_command('off-v1', self.work)[-2:],
                         ['--startup-plan', 'off'])
        self.assertEqual(run.server_command('deploy-plain-v1', self.work)[-1], '--no-mtp')

    def test_offload_environment_preserves_old_contract(self):
        for stage in ('c256-hot-v1', 'c256-inherited-v1', 'boundary-v2'):
            cfg = run.configuration(stage)
            env = run.environment(stage, self.work, {'Q4T_BAD': '1'})
            expected = dict(run.BASE_ENVIRONMENT, Q4T_MOE_CHUNK_ORDER='0',
                            Q4T_MOE_L2_SLOTS=str(cfg['l2_slots']),
                            Q4T_MOE_MIRROR_K=str(cfg['mirror_k']),
                            Q4T_MOE_LOAD_THREADS=str(cfg['load_threads']))
            self.assertEqual(env, expected)
            self.assertEqual(cfg['memory_max'], 64 << 30)

    def test_capacity_accepts_budget_derived_default(self):
        result = run.capacity_evidence(log_for('default-cold-v1'), 'default-cold-v1')
        self.assertEqual(result['requested']['max_len'], 0)
        self.assertEqual(result['effective']['max_len'], 131072)

    def test_explicit_capacity_capping_rejected(self):
        log = log_for('c256-hot-v1').replace('effective_max_len=262144',
                                           'effective_max_len=131072')
        with self.assertRaises(RuntimeError):
            run.capacity_evidence(log, 'c256-hot-v1')

    def test_mtp_fallback_rejected(self):
        log = log_for('default-cold-v1').replace('MTP loaded', 'MTP load failed')
        with self.assertRaises(RuntimeError):
            run.capacity_evidence(log, 'default-cold-v1')

    def test_summary_ready_and_final(self):
        self.assertFalse(run.reservation_evidence(log_for('default-cold-v1'),
                         'default-cold-v1')['finalized'])
        self.assertTrue(run.reservation_evidence(log_for('default-cold-v1', True),
                         'default-cold-v1', finalized=True)['finalized'])

    def test_summary_unknown_release_rejected(self):
        log = log_for('default-cold-v1', True).replace('"release_unknown": 0',
                                                   '"release_unknown": 1')
        with self.assertRaises(RuntimeError):
            run.reservation_evidence(log, 'default-cold-v1', finalized=True)

    def test_summary_partial_final_free_rejected(self):
        log = log_for('default-cold-v1', True).replace('"freed": 2', '"freed": 1')
        with self.assertRaises(RuntimeError):
            run.reservation_evidence(log, 'default-cold-v1', finalized=True)

    def test_summary_reprepare_rejected(self):
        log = log_for('default-cold-v1')
        log += '[startup-reservation] ' + json.dumps(summary_rows()[0]) + '\n'
        with self.assertRaises(RuntimeError):
            run.reservation_evidence(log, 'default-cold-v1')

    def test_summary_complete_failed_rejected(self):
        log = log_for('default-cold-v1').replace('"event": "startup_complete"',
                                               '"event": "complete_failed"')
        with self.assertRaises(RuntimeError):
            run.reservation_evidence(log, 'default-cold-v1')

    def test_off_rejects_any_reservation(self):
        log = log_for('off-v1')
        self.assertFalse(run.reservation_evidence(log, 'off-v1')['active'])
        log += '[startup-reservation] ' + json.dumps(summary_rows()[0]) + '\n'
        with self.assertRaises(RuntimeError):
            run.reservation_evidence(log, 'off-v1')

    def test_research_trace_rejected(self):
        with self.assertRaises(RuntimeError):
            run.reservation_evidence(log_for('off-v1') + '[memory-trace] {}\n', 'off-v1')

    def test_inherited_cache_is_observation_only(self):
        observation = dict(complete_file_set_observed=True, errors=[],
            files=[dict(path='/fixture/model.safetensors', resident_bytes=4096)])
        with patch.object(run, 'model_files', return_value=[]), \
             patch.object(run, 'observe_files', return_value=observation) as observer, \
             patch.object(run, 'prepare_target_cache', side_effect=AssertionError('eviction')), \
             patch.object(run, 'clear_target_cache', side_effect=AssertionError('eviction')), \
             patch.object(run, 'LockedWarm', side_effect=AssertionError('locking')):
            run.cache_gate('default-inherited-v1', self.work)
        self.assertEqual(observer.call_count, 2)
        gate = run.read(self.work / 'cache-gate.json')
        self.assertTrue(gate['naturally_inherited'])
        self.assertFalse(gate['file_vector_exact'])
        self.assertEqual(gate['payload_bytes'], 4096)

    def test_cold_cache_one_pass_no_retry(self):
        observation = dict(complete_file_set_observed=True, errors=[], files=[])
        with patch.object(run, 'model_files', return_value=[]), \
             patch.object(run, 'observe_files', return_value=observation), \
             patch.object(run, 'clear_target_cache', return_value=[]) as prep:
            run.cache_gate('default-cold-v1', self.work)
        prep.assert_called_once_with([])

    def test_cold_nonzero_cache_rejects(self):
        observation = dict(complete_file_set_observed=True, errors=[],
            files=[dict(path='/fixture/model.safetensors', resident_bytes=4096)])
        with patch.object(run, 'model_files', return_value=[Path('/fixture/model.safetensors')]), \
             patch.object(run, 'observe_files', return_value=observation), \
             patch.object(run, 'clear_target_cache', return_value=[]) as advice:
            with self.assertRaises(RuntimeError):
                run.cache_gate('default-cold-v1', self.work)
        self.assertEqual(advice.call_count, 2)

    def cold_fixture(self, amounts=(4096, 0, 512), *, complete=True, errors=None):
        paths = [self.work / n for n in ('a.safetensors', 'b.bin', 'config.json')]
        return paths, dict(complete_file_set_observed=complete, errors=errors or [],
            files=[dict(path=str(path), resident_bytes=amount)
                   for path, amount in zip(paths, amounts)])

    def test_cold_second_pass_only_known_payload_residue(self):
        paths, first = self.cold_fixture()
        _, last = self.cold_fixture((0, 0, 512))
        with patch.object(run, 'model_files', return_value=paths), \
             patch.object(run, 'observe_files', side_effect=[first, first, last, last]) as observe, \
             patch.object(run, 'clear_target_cache', return_value=[]) as advice, \
             patch.object(run.time, 'sleep') as sleep:
            run.cache_gate('boundary-v2', self.work)
        self.assertEqual(advice.call_args_list[0].args, (paths,))
        self.assertEqual(advice.call_args_list[1].args, ([paths[0]],))
        self.assertEqual(observe.call_count, 4)
        self.assertTrue(all(c.args == (paths,) for c in observe.call_args_list))
        gate = run.read(self.work / 'cache-gate.json')
        self.assertEqual(gate['preparation_passes'], 2)
        self.assertEqual(gate['residual_retry_files'], [str(paths[0])])
        self.assertEqual(gate['payload_bytes'], 0)
        sleep.assert_called_once_with(.1)

    def test_cold_unknown_observation_never_retries(self):
        paths, unknown = self.cold_fixture(complete=False, errors=[{'errno': 5}])
        with patch.object(run, 'observe_files', return_value=unknown), \
             patch.object(run, 'clear_target_cache', return_value=[]) as advice, \
             patch.object(run.time, 'sleep') as sleep:
            with self.assertRaises(RuntimeError):
                run.prepare_cold_cache(paths, self.work)
        advice.assert_called_once_with(paths)
        sleep.assert_not_called()
        self.assertEqual(len(run.read(self.work / 'cache-preparation.json')['attempts']), 1)

    def test_cold_advice_error_never_retries(self):
        paths, first = self.cold_fixture()
        with patch.object(run, 'observe_files', return_value=first), \
             patch.object(run, 'clear_target_cache', return_value=[{'errno': 5}]) as advice, \
             patch.object(run.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'advice failed'):
                run.prepare_cold_cache(paths, self.work)
        advice.assert_called_once_with(paths)
        sleep.assert_not_called()

    def test_cold_changed_file_set_never_retries(self):
        paths, first = self.cold_fixture()
        first['files'].pop()
        with patch.object(run, 'observe_files', return_value=first), \
             patch.object(run, 'clear_target_cache', return_value=[]) as advice:
            with self.assertRaisesRegex(RuntimeError, 'file set/residency unknown'):
                run.prepare_cold_cache(paths, self.work)
        advice.assert_called_once_with(paths)

    def test_actual_retained_failure_is_bound_and_rejected(self):
        failure = run.retained_failure()
        self.assertEqual(failure['name'], 'boundary-v1')
        self.assertEqual((failure['model_starts'], failure['http_requests'],
                          failure['preparations']), (0, 0, 1))

    def test_retained_failure_cannot_be_relabelled_accepted(self):
        original = run.read
        def changed(path):
            result = original(path)
            if Path(path).name == 'audit-boundary-v1.json':
                result = copy.deepcopy(result)
                result['runtime_accepted'] = True
            return result
        with patch.object(run, 'read', side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, 'retained failure was accepted'):
                run.retained_failure()

    def test_v2_requires_retained_failure_and_keeps_budget(self):
        candidate = run.sha(run.BINARY)
        def prior(name):
            return dict(name=name, model_starts=1,
                        http_requests=run.configuration(name)['requests'],
                        candidate_sha256=candidate)
        failure = dict(name='boundary-v1', model_starts=0, http_requests=0,
                       preparations=1)
        with patch.object(run, 'BASE', self.work), \
             patch.object(run, 'sealed_attempt', side_effect=prior), \
             patch.object(run, 'retained_failure', return_value=failure):
            boundary = run.previous_stage_gate('boundary-v2')
            off = run.previous_stage_gate('off-v1')
        self.assertEqual(boundary['prior_preparations'], 5)
        self.assertEqual(off['prior_preparations'], 6)
        self.assertEqual((off['model_start_limit'], off['http_limit'],
                          off['preparation_limit']), (6, 9, 7))
        with patch.object(run, 'BASE', self.work), \
             patch.object(run, 'sealed_attempt', side_effect=prior):
            with self.assertRaises(FileNotFoundError):
                run.previous_stage_gate('boundary-v2')

    def test_future_v3_attempt_is_forbidden(self):
        with self.assertRaises(RuntimeError):
            run.configuration('boundary-v3')
        (self.work / 'boundary-v3').mkdir()
        with patch.object(run, 'BASE', self.work):
            with self.assertRaisesRegex(RuntimeError, 'unapproved additional'):
                run.previous_stage_gate('boundary-v2')

    def test_no_stage_retry(self):
        (self.work / run.STAGES[0]).mkdir()
        with patch.object(run, 'BASE', self.work):
            with self.assertRaises(RuntimeError):
                run.previous_stage_gate(run.STAGES[0])

    def test_prior_failure_blocks_next(self):
        with patch.object(run, 'BASE', self.work), \
             patch.object(run, 'sealed_attempt', side_effect=RuntimeError('prior failed')):
            with self.assertRaisesRegex(RuntimeError, 'prior failed'):
                run.previous_stage_gate(run.STAGES[1])

    def test_guard_env_abort_with_completed_cleanup_retained(self):
        (self.work / 'lease').mkdir()
        value = dict(status='ENV_ABORT', finished=dict(monotonic=1.0),
                     reason='query timeout')
        run.save(self.work / 'lease/exit.json', value)
        self.assertEqual(run.guard_cleanup_receipt(self.work), value)

    def test_guard_cleanup_error_requires_recovery(self):
        (self.work / 'lease').mkdir()
        run.save(self.work / 'lease/exit.json', dict(status='ENV_ABORT',
                 finished=dict(monotonic=1.0), cleanup_error='descendant remains'))
        with self.assertRaisesRegex(RuntimeError, 'cleanup is unconfirmed'):
            run.guard_cleanup_receipt(self.work)

    def test_guard_missing_receipt_requires_recovery(self):
        with self.assertRaises(FileNotFoundError):
            run.guard_cleanup_receipt(self.work)

    def test_guard_unknown_cleanup_does_not_skip_scope_or_collector(self):
        manifest = self.work / 'build.json'
        run.save(manifest, dict(candidate_sha256='a' * 64))
        lock_path = self.work / 'owned.lock'
        processes = []

        class Process:
            def __init__(self, command, **_kwargs):
                self.guard = 'gpu_exclusive.py' in ' '.join(command)
                self.pid = 424242 + len(processes)
                self.returncode = 75 if self.guard else None
                self.waited = False
                folder = Path(command[command.index('--output') + 1])
                folder.mkdir(parents=True)
                value = (dict(status='ENV_ABORT', cleanup_error='unknown child',
                              finished=dict(monotonic=1.0)) if self.guard else
                         dict(status='COMPLETED', failures=[], truncated=False))
                run.save(folder / 'exit.json', value)
                processes.append(self)

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                self.waited = True
                self.returncode = 75 if self.guard else 0
                return self.returncode

        with patch.object(run, 'BASE', self.work), \
             patch.object(run, 'BUILD_MANIFEST', manifest), \
             patch.object(run, 'verify_build', return_value=dict(candidate_sha256='a' * 64)), \
             patch.object(run, 'previous_stage_gate', return_value={}), \
             patch.object(run, 'common_lock', return_value=lock_path), \
             patch.object(Path, 'glob', return_value=[]), \
             patch.object(run.subprocess, 'Popen', Process), \
             patch.object(run, 'wait_ready'), \
             patch.object(run, 'cleanup_scope', return_value=dict(scope_created=False)) as scope, \
             patch.object(run, 'helper_cleanup_evidence', return_value=dict(started=False)):
            self.assertEqual(run.supervise('default-cold-v1'), 1)
        state = run.read(self.work / 'default-cold-v1/state.json')
        self.assertEqual(state['status'], 'RECOVERY_REQUIRED')
        scope.assert_called_once()
        self.assertTrue(processes[0].waited)
        self.assertTrue(lock_path.with_name('owned.lock.recovery-required').exists())

    def exercise_http(self, *, fail_log=False, bad_quality=False):
        target = self.work / 'run'
        target.mkdir()
        binary = self.work / 'fake-candidate'
        binary.write_text('cpu fixture; never executed')
        scopes = []
        stage = 'default-cold-v1'

        class Scope:
            def __init__(self, _command, **kwargs):
                self.path = kwargs['log_path']
                self.path.write_text(log_for(stage))
                self.pid, self.returncode = 424242, None
                self.terminated = self.closed = False
                scopes.append(self)

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.path.write_text(log_for(stage, True))

            def wait(self, timeout):
                self.returncode = 0
                return 0

            def close(self):
                self.closed = True

        def fixtures(path):
            (path / 'fixtures').mkdir()
            run.save(path / 'fixtures/manifest.json', [dict(id='fixture',
                     expected='710003', prompt_sha256='b' * 64)])
            (path / 'fixtures/requests.jsonl').write_text('{}\n')

        count = 0
        def fake_case(*_args, **_kwargs):
            nonlocal count
            count += 1
            return [dict(text='bad' if bad_quality else '710003', actual_input=1024,
                         actual_output=7, finish=['stop'], prompt_sha256='b' * 64,
                         response_id='response-' + str(count))]

        def fake_event(_out, name, **_kwargs):
            if fail_log and name == 'server_before_shutdown':
                raise OSError('fixture full log')

        with patch.object(run, 'prepare_fixtures', side_effect=fixtures), \
             patch.object(run, 'cache_gate'), patch.object(run, 'IsolatedScope', Scope), \
             patch.object(run, 'run_case', side_effect=fake_case), \
             patch.object(run, 'event', side_effect=fake_event):
            rc = run.http_worker(stage, run=target, binary=binary,
                                 verify_callback=lambda: None)
        self.assertEqual(len(scopes), 1)
        self.assertTrue(scopes[0].terminated and scopes[0].closed)
        return rc, run.read(target / 'normal/http/exit.json')

    def test_http_reuse_and_release(self):
        rc, result = self.exercise_http()
        self.assertEqual(rc, 0)
        self.assertEqual(result['completed'], 2)
        self.assertTrue(result['http_output_checks_passed'])

    def test_http_quality_failure_still_cleans(self):
        rc, result = self.exercise_http(bad_quality=True)
        self.assertEqual(rc, 1)
        self.assertFalse(result['http_output_checks_passed'])
        self.assertEqual(result['completed'], 1)

    def test_shutdown_log_failure_still_cleans(self):
        rc, result = self.exercise_http(fail_log=True)
        self.assertEqual(rc, 1)
        self.assertTrue(result['logging_errors'])
        self.assertFalse(result['http_output_checks_passed'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--test', action='append', default=[],
                        help='rerun only a named affected contract')
    args = parser.parse_args()
    output = args.output.resolve()
    run.require(output.is_relative_to(run.BASE) and not output.exists(),
                'new delivery evidence directory required')
    output.mkdir(parents=True)
    Contracts.output = output
    names = args.test or unittest.defaultTestLoader.getTestCaseNames(Contracts)
    run.require(len(set(names)) == len(names) and all(name in
                unittest.defaultTestLoader.getTestCaseNames(Contracts)
                for name in names), 'unknown or duplicate selected contract')
    with (output / 'stdout.log').open('x') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
            unittest.TestSuite(Contracts(name) for name in names))
    run.save(output / 'result.json', dict(status='PASS' if result.wasSuccessful()
        else 'FAILED', tests=result.testsRun, gpu_execution=False, host_substitutes=True,
        actual_cache_operations=False, argv=sys.argv, selected_tests=names,
        sources={str(p): run.build_tools.identity(p)
                 for p in (HERE / 'run.py', Path(__file__))}))
    print(json.dumps(dict(status='PASS' if result.wasSuccessful() else 'FAILED',
                         tests=result.testsRun, output=str(output))))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
