"""CPU driver contracts; all cache, lock, server and GPU actions are stubs."""
from contextlib import ExitStack
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('startup_cache_runner',
                                            HERE / 'run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def snapshot(path='/test.scope', valid=True, status='OBSERVED'):
    files = {name: {'value': value} for name, value in (
        ('memory.current', 1024), ('memory.max', 'max'),
        ('memory.high', 'max'))}
    return dict(identity_valid=valid, data_complete=False,
                cgroup_before={'value': path}, ancestors=[
                    dict(cgroup=path, status=status, files=files),
                    dict(cgroup='/', status='PARTIAL_UNKNOWN', files={})])


class DriverContracts(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.q4t-work/startup-cache-20261021/runner-host'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # An accidental call cannot start a child, GPU, scope or real probe.
        for name in ('prepare_target_cache', 'observe_files', 'LockedWarm',
                     'IsolatedScope', 'run_case', 'run_owned_runner'):
            self.stack.enter_context(patch.object(
                runner, name, side_effect=AssertionError('unstubbed ' + name)))
        self.stack.enter_context(patch.object(
            runner.subprocess, 'Popen',
            side_effect=AssertionError('no process')))
        self.stack.enter_context(patch.object(
            runner.subprocess, 'run', side_effect=AssertionError('no command')))

    def test_matrix_has_three_starts_four_requests_and_default_mtp(self):
        self.assertEqual(runner.STAGES,
                         ('mtp-lock-v3', 'restart-lock-v3', 'boundary-lock-v3'))
        self.assertEqual([runner.configuration(s)['requests']
                          for s in runner.STAGES], [2, 1, 1])
        for stage in runner.STAGES:
            cfg = runner.configuration(stage)
            command = runner.server_command(stage, self.out)
            self.assertNotIn('--mtp', command)
            self.assertNotIn('--mtp-k', command)
            self.assertEqual('--no-mtp' in command, not cfg['mtp'])
            self.assertEqual(command[0], str(runner.BINARY))

    def test_boundary_configuration_and_environment_are_frozen(self):
        cfg = runner.configuration('boundary-lock-v3')
        self.assertEqual(tuple(cfg[key] for key in ('max_seq', 'max_prefill',
                         'max_len', 'resident_slots', 'load_threads',
                         'l2_slots', 'mirror_k')),
                         (2, 2048, 8192, 128, 1, 1, 0))
        env = runner.environment('boundary-lock-v3', self.out,
                                 {'Q4T_UNTESTED': '1', 'LD_PRELOAD': 'x'})
        self.assertNotIn('Q4T_UNTESTED', env)
        self.assertNotIn('LD_PRELOAD', env)
        self.assertEqual(env['Q4T_MOE_L2_SLOTS'], '1')
        self.assertEqual(env['Q4T_MOE_LOAD_THREADS'], '1')

    def test_snapshot_accepts_explicit_root_unknown_only(self):
        who = dict(pid=123, start_ticks=456)
        with patch.object(runner, 'observe_process', return_value=snapshot()) \
                as observe:
            result = runner.process_snapshot(self.out, 'ready', who)
        observe.assert_called_once_with(123, 456)
        self.assertFalse(result['data_complete'])
        self.assertTrue((self.out / 'ready-cgroup.json').exists())

    def test_snapshot_rejects_identity_and_nonroot_unknown(self):
        for value in (snapshot(valid=False), snapshot(status='PARTIAL_UNKNOWN'),
                      dict(identity_valid=True, ancestors=[])):
            with patch.object(runner, 'observe_process', return_value=value):
                with self.assertRaises(RuntimeError):
                    runner.process_snapshot(self.out, 'bad',
                                            dict(pid=123, start_ticks=456))

    def test_lock_budget_requires_all_ancestor_headroom(self):
        good = snapshot()
        ancestor = copy.deepcopy(good['ancestors'][0])
        ancestor['cgroup'] = '/ancestor'
        ancestor['files']['memory.max']['value'] = runner.HOT_BYTES
        good['ancestors'].insert(1, ancestor)
        with self.assertRaisesRegex(RuntimeError, 'headroom'):
            runner.check_lock_budget(good, runner.HOT_BYTES)
        with self.assertRaisesRegex(RuntimeError, 'frozen full hot'):
            runner.check_lock_budget(snapshot(), 0)

    def gate_fixture(self, *, fail=None, drift=False, released=None):
        run = self.out / ('gate-' + str(len(list(self.out.glob('gate-*')))))
        run.mkdir()
        model = run / 'model'
        model.mkdir()
        paths = [model / f'layer-{i:05d}-experts-0000-0127.safetensors'
                 for i in range(2)]
        for path in paths:
            path.write_bytes(b'CPU fixture metadata only')
        expected = runner.expected_vector(paths, paths)
        total = sum(expected.values())
        observation = dict(complete_file_set_observed=True,
            files=[dict(path=p, resident_bytes=b) for p, b in expected.items()])
        manifest = dict(total_lock_bytes=total,
            files=[dict(path=p, lock_bytes=b) for p, b in expected.items()])
        events = []
        owner = dict(pid=12, start_ticks=34, memlock=[8192, 8192],
                     vmlck_bytes=0)
        outer = self

        class Helper:
            def __init__(self, *_args, **_kwargs):
                self.identity = owner
                events.append('ready')

            def grant(self, **kwargs):
                outer.assertTrue(kwargs['use_sudo'])
                events.append('grant')
                if fail == 'grant':
                    raise RuntimeError('fixture grant failure')

            def acquire(self):
                events.append('acquire')
                if fail == 'acquire':
                    raise RuntimeError('fixture acquire failure')

            def release(self):
                events.append('release')
                if fail == 'release':
                    raise RuntimeError('fixture release failure')
                return released if released is not None else dict(
                    release_success=True, reaped=True, returncode=0)

            def close(self):
                events.append('close')

        observations = [copy.deepcopy(observation) for _ in range(3)]
        if drift:
            observations[-1]['files'][0]['resident_bytes'] = 0

        def observe(_paths):
            events.append('observe')
            return observations.pop(0)

        def process(_run, name, _identity):
            events.append(name)
            if fail == name:
                raise RuntimeError('fixture cgroup failure')
            return snapshot('/moved.scope' if fail == 'moved' and
                            name == 'helper-held' else '/test.scope')

        for name, value in (('MODEL', model), ('HOT_BYTES', total),
                            ('LockedWarm', Helper)):
            self.stack.enter_context(patch.object(runner, name, value))
        for name, value in (('model_files', paths),
                            ('verified_cache_reference', expected),
                            ('make_manifest', manifest),
                            ('prepare_target_cache', ([], {}, 1)),
                            ('payload_residual', 0), ('check_lock_budget', {}),
                            ('identity', owner)):
            self.stack.enter_context(patch.object(runner, name,
                                                  return_value=value))
        self.stack.enter_context(patch.object(runner, 'observe_files',
                                              side_effect=observe))
        self.stack.enter_context(patch.object(runner, 'process_snapshot',
                                              side_effect=process))
        return run, events

    def test_single_lock_releases_before_final_exact_gate(self):
        run, events = self.gate_fixture()
        runner.final_cache_gate(run)
        self.assertEqual(events.count('ready'), 1)
        self.assertEqual(events.count('acquire'), 1)
        self.assertEqual(events.count('release'), 1)
        self.assertEqual(events[-1], 'observe')
        self.assertLess(events.index('release'), events.index('close'))
        self.assertLess(events.index('close'), len(events) - 1)
        gate = runner.read(run / 'cache-gate.json')
        self.assertTrue(gate['temporary_locks_released'])
        self.assertTrue(gate['file_vector_exact'])
        self.assertEqual(gate['repair_passes'], 0)
        timing = runner.read(run / 'cache-launch-time.json')
        self.assertLessEqual(timing['lock_owner_reaped_mono'],
                             timing['scan_begin_mono'])
        self.assertLessEqual(timing['scan_begin_mono'], timing['scan_end_mono'])

    def test_post_unlock_drift_has_no_retry_or_success_gate(self):
        run, events = self.gate_fixture(drift=True)
        with self.assertRaisesRegex(RuntimeError, 'cache vector differs'):
            runner.final_cache_gate(run)
        self.assertEqual(events.count('acquire'), 1)
        self.assertEqual(events.count('release'), 1)
        self.assertFalse((run / 'cache-gate.json').exists())
        self.assertTrue((run / 'cache-launch.json').exists())

    def test_helper_and_cgroup_failure_always_close_without_final_gate(self):
        for phase in ('grant', 'acquire', 'release', 'helper-ready',
                      'helper-held', 'moved'):
            with self.subTest(phase=phase):
                run, events = self.gate_fixture(fail=phase)
                with self.assertRaises(RuntimeError):
                    runner.final_cache_gate(run)
                self.assertEqual(events[-1], 'close')
                self.assertFalse((run / 'cache-launch.json').exists())
                self.assertFalse((run / 'cache-gate.json').exists())

    def test_unconfirmed_release_does_not_pass(self):
        for field in ('release_success', 'reaped'):
            result = dict(release_success=True, reaped=True, returncode=0)
            result[field] = False
            run, events = self.gate_fixture(released=result)
            with self.assertRaisesRegex(RuntimeError, 'release unconfirmed'):
                runner.final_cache_gate(run)
            self.assertEqual(events[-1], 'close')
            self.assertFalse((run / 'cache-gate.json').exists())

    def test_nonzero_helper_exit_cannot_pass_release_flags(self):
        run, events = self.gate_fixture(released=dict(
            release_success=True, reaped=True, returncode=125))
        with self.assertRaisesRegex(RuntimeError, 'release unconfirmed'):
            runner.final_cache_gate(run)
        self.assertEqual(events[-1], 'close')
        self.assertFalse((run / 'cache-launch.json').exists())

    def http_fixture(self, stage='mtp-lock-v3', gate_error=False):
        base = self.out / 'http'
        run = base / stage / 'run'
        fixtures = run / 'fixtures'
        fixtures.mkdir(parents=True)
        runner.save(fixtures / 'manifest.json', [dict(
            id='fixture', expected='710003', prompt_sha256='prompt')])
        (fixtures / 'requests.jsonl').write_text('{}\n')
        binary = self.out / 'q4t'
        binary.write_bytes(b'non executable CPU fixture')
        binary.with_name('CMakeCache.txt').write_text('fixture')
        events = []

        def gate(_run):
            events.append('release_exit0_and_exact')
            if gate_error:
                raise RuntimeError('fixture gate failed')

        class Server:
            pid, returncode = 123, None

            def __init__(self, _command, **kwargs):
                events.append('server_start')
                kwargs['log_path'].write_text('[q4t] serving on port 8841\n')

            def terminate(self):
                events.append('terminate')
                self.returncode = 0

            def wait(self, **_kwargs):
                return self.returncode

            def close(self):
                events.append('server_close')

        def request(*_args):
            events.append('http')
            return [dict(text='710003', actual_input=1024, actual_output=7,
                         finish=['stop'], prompt_sha256='prompt',
                         response_id=str(events.count('http')))]

        for name, value in (('BASE', base), ('BINARY', binary),
                            ('IsolatedScope', Server)):
            self.stack.enter_context(patch.object(runner, name, value))
        for name, value in (('verify_build', {}), ('capacity_evidence', {}),
                            ('check_plan', {}), ('retained_reservation', {}),
                            ('reservation_evidence', {'status': 'PASS'})):
            self.stack.enter_context(patch.object(runner, name,
                                                  return_value=value))
        self.stack.enter_context(patch.object(runner, 'final_cache_gate',
                                              side_effect=gate))
        self.stack.enter_context(patch.object(runner, 'run_case',
                                              side_effect=request))
        return run, events

    def test_failed_gate_never_constructs_server_or_http(self):
        run, events = self.http_fixture(gate_error=True)
        self.assertEqual(runner.http_worker('mtp-lock-v3'), 1)
        self.assertEqual(events, ['release_exit0_and_exact'])
        result = runner.read(run / 'normal/http/exit.json')
        self.assertFalse(result['http_output_checks_passed'])
        self.assertEqual(result['completed'], 0)

    def test_mtp_two_requests_follow_gate_and_reuse_one_server(self):
        run, events = self.http_fixture()
        self.assertEqual(runner.http_worker('mtp-lock-v3'), 0)
        self.assertEqual(events, ['release_exit0_and_exact', 'server_start',
                                  'http', 'http', 'terminate', 'server_close'])
        self.assertTrue(runner.read(run / 'normal/http/exit.json')
                        ['http_output_checks_passed'])

    def history_fixture(self, stage='mtp-lock-v3'):
        base, history = self.out / 'new', self.out / 'history'
        base.mkdir()
        history.mkdir()
        binary = self.out / 'candidate'
        binary.write_bytes(b'candidate identity')
        runner.save(history / 'build.json', {'candidate_sha256': 'old'})
        self.stack.enter_context(patch.object(runner, 'BASE', base))
        self.stack.enter_context(patch.object(runner, 'HISTORY_BASE', history))
        self.stack.enter_context(patch.object(runner, 'BINARY', binary))
        original = runner.sealed_attempt
        self.stack.enter_context(patch.object(runner, 'sealed_attempt',
            side_effect=lambda name, root=None, **kwargs: original(
                name, base if root is None else root, **kwargs)))
        for name in runner.HISTORY:
            self.write_attempt(history, name,
                passed=name == 'plain-v1',
                started=name in ('plain-v1', 'mtp-v3'),
                requests=int(name == 'plain-v1'),
                guard='ENV_ABORT' if name == 'mtp-v3' else 'COMMAND_FAILED',
                candidate=(runner.sha(binary)
                           if name == 'mtp-order-v1' else 'old'))
        interrupted = {}
        for name in ('mtp-lock-v1', 'mtp-lock-v2'):
            out = self.write_attempt(base, name, passed=False, started=True,
                requests=0, guard='ENV_ABORT', candidate=runner.sha(binary),
                collector_interruption=True)
            interrupted[name] = runner.sha(out / 'sealed-files.json')
        self.stack.enter_context(patch.object(runner, 'INTERRUPTED',
                                              interrupted))
        for name in runner.STAGES[:runner.STAGES.index(stage)]:
            self.write_attempt(base, name, passed=True, started=True,
                requests=runner.configuration(name)['requests'],
                candidate=runner.sha(binary))
        seal = history / 'final-evidence.json'
        names = ['build.json'] + [name + '/sealed-files.json'
                                  for name in runner.HISTORY]
        runner.save(seal, {name: runner.sha(history / name) for name in names})
        self.stack.enter_context(patch.object(runner, 'HISTORY_SEAL_SHA',
                                              runner.sha(seal)))
        return base, history

    def write_attempt(self, root, name, *, passed, started, requests,
                      candidate, guard='COMPLETED',
                      collector_interruption=False):
        out = root / name
        http = out / 'run/normal/http'
        http.mkdir(parents=True)
        (out / 'lease').mkdir()
        collector = dict(returncode=0, result={'status': 'COMPLETED'})
        state = dict(default_unchanged=True,
            status='RUNTIME_COMPLETED' if passed else 'RUNTIME_NOT_ACCEPTED',
            resources=collector)
        if name in runner.STAGES:
            state.update(collector_profile=runner.COLLECTOR_PROFILE,
                         kernel_timeline='NOT_COLLECTED')
        else:
            state['kernel'] = copy.deepcopy(collector)
        if collector_interruption:
            kernel = dict(status='FAILED', instance_removed=True,
                          global_configuration_equal=True)
            if name == 'mtp-lock-v1':
                kernel.update(reason='CaptureFailure: trace byte limit reached',
                    max_bytes=536870912, trace_bytes=536870912,
                    trace_unwritten_at_least=1)
            else:
                kernel.update(reason='CaptureFailure: trace events lost: '
                    '{"overrun": 5501, "commit overrun": 0, '
                    '"dropped events": 0}',
                    max_bytes=2147483648, loss_totals={'overrun': 5501})
            state.update(status='FAILED',
                error="RuntimeError('collector exited during guarded stage')",
                kernel=dict(returncode=1, result=kernel),
                scope_final={'after': {'ActiveState': 'inactive'}},
                lock_owner_cleanup={'live_lock_owner': False})
        runner.save(out / 'state.json', state)
        runner.save(out / 'lease/exit.json',
                    {'status': 'COMPLETED' if passed else guard})
        runner.save(out / 'build.json', {'candidate_sha256': candidate})
        rows = ([{'event': 'server_started'}] if started else [])
        rows += [{'event': 'client_before'} for _ in range(requests)]
        (http / 'events.jsonl').write_text(''.join(
            json.dumps(row) + '\n' for row in rows))
        runner.save(http / 'exit.json', {'http_output_checks_passed': passed})
        runner.save(http / 'reservation-audit.json',
                    {'status': 'PASS' if passed else 'FAILED'})
        runner.save(out / 'run/cache-gate.json', dict(
            payload_bytes=runner.HOT_BYTES, file_vector_exact=True,
            temporary_locks_released=True, physical_fragmentation_equal=False,
            repair_passes=0))
        (out / 'run/locked-warm').mkdir()
        runner.save(out / 'run/locked-warm/exit.json', dict(
            release_success=passed, reaped=True, returncode=0))
        self.reseal(out)
        return out

    def reseal(self, out):
        runner.save(out / 'sealed-files.json', {
            str(path.relative_to(out)): runner.sha(path)
            for path in out.rglob('*')
            if path.is_file() and path.name != 'sealed-files.json'})

    def test_history_retains_failed_preparations_and_remaining_budget(self):
        self.history_fixture()
        result = runner.previous_stage_gate('mtp-lock-v3')
        self.assertEqual(len(result['prior']), 7)
        self.assertEqual((result['prior_model_starts'],
                          result['prior_http_requests']), (4, 1))
        self.assertEqual((result['planned_model_starts'],
                          result['planned_http_requests']), (1, 2))
        self.assertEqual((result['model_start_limit'], result['http_limit']),
                         (7, 5))
        self.assertEqual((result['preparation_limit'],
                          result['prior_new_preparations'],
                          result['collector_interruptions_retained']),
                         (5, 2, 2))
        self.assertEqual(result['collector_profile'],
                         'resources_nvml_application_ledger')
        self.assertTrue(result['no_retry'])

    def test_final_boundary_consumes_exact_seven_start_five_http_limits(self):
        self.history_fixture('boundary-lock-v3')
        result = runner.previous_stage_gate('boundary-lock-v3')
        self.assertEqual((result['prior_model_starts'],
                          result['prior_http_requests']), (6, 4))
        self.assertEqual(result['prior_new_preparations'], 4)
        self.assertEqual(result['planned_http_requests'], 1)

    def test_no_retry_or_skipping_failed_previous_stage(self):
        base, _history = self.history_fixture()
        (base / 'mtp-lock-v3').mkdir()
        with self.assertRaisesRegex(RuntimeError, 'no implicit retry'):
            runner.previous_stage_gate('mtp-lock-v3')

    def test_prior_new_stage_must_release_exactly_and_pass(self):
        base, _history = self.history_fixture('restart-lock-v3')
        previous = base / 'mtp-lock-v3'
        gate = previous / 'run/cache-gate.json'
        value = runner.read(gate)
        value['temporary_locks_released'] = False
        runner.save(gate, value)
        self.reseal(previous)
        with self.assertRaisesRegex(RuntimeError, 'previous new stage'):
            runner.previous_stage_gate('restart-lock-v3')
        value['temporary_locks_released'] = True
        runner.save(gate, value)
        runner.save(previous / 'run/locked-warm/exit.json', dict(
            release_success=False, reaped=True, returncode=0))
        self.reseal(previous)
        with self.assertRaisesRegex(RuntimeError, 'previous new stage'):
            runner.previous_stage_gate('restart-lock-v3')

    def test_resealing_history_cannot_replace_frozen_anchor(self):
        _base, history = self.history_fixture()
        old = history / 'plain-v1'
        state = old / 'state.json'
        state.write_text(state.read_text() + '\n')
        self.reseal(old)
        with self.assertRaisesRegex(RuntimeError, 'historical identity'):
            runner.previous_stage_gate('mtp-lock-v3')
        seal = history / 'final-evidence.json'
        seal.write_text(seal.read_text() + '\n')
        with self.assertRaisesRegex(RuntimeError, 'historical final seal'):
            runner.previous_stage_gate('mtp-lock-v3')

    def test_historical_top_level_build_is_also_frozen(self):
        _base, history = self.history_fixture()
        runner.save(history / 'build.json', {'candidate_sha256': 'changed'})
        with self.assertRaisesRegex(RuntimeError, 'historical identity'):
            runner.previous_stage_gate('mtp-lock-v3')

    def test_seal_rejects_mutation_and_added_unsealed_file(self):
        base, history = self.history_fixture()
        target = history / 'plain-v1/state.json'
        target.write_text(target.read_text() + '\n')
        with self.assertRaisesRegex(RuntimeError, 'sealed evidence changed'):
            runner.previous_stage_gate('mtp-lock-v3')
        (history / 'mtp-v1/unsealed').write_text('unexpected')
        with self.assertRaisesRegex(RuntimeError, 'file set changed'):
            runner.sealed_attempt('mtp-v1', history)

    def test_budget_cannot_be_expanded_by_new_stage(self):
        self.history_fixture()
        with patch.object(runner, 'HTTP_LIMIT', 2):
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                runner.previous_stage_gate('mtp-lock-v3')
        with patch.object(runner, 'MODEL_START_LIMIT', 2):
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                runner.previous_stage_gate('mtp-lock-v3')

    def test_cleanup_missing_or_pre_ready_failure_is_explicit(self):
        self.assertEqual(runner.helper_cleanup_evidence(self.out),
                         dict(started=False, live_lock_owner=False))
        path = self.out / 'run/locked-warm/child-events.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'event': 'summary'}) + '\n')
        self.assertFalse(runner.helper_cleanup_evidence(self.out)['started'])
        path.write_text(json.dumps({'event': 'mlock'}) + '\n')
        with self.assertRaisesRegex(RuntimeError, 'without birth identity'):
            runner.helper_cleanup_evidence(self.out)

    def test_cleanup_requires_same_birth_identity_gone(self):
        path = self.out / 'run/locked-warm/child-events.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(event='ready',
            identity=dict(pid=123, start_ticks=456))) + '\n')
        original = Path.read_text

        def read(target, *args, **kwargs):
            if target == Path('/proc/123/stat'):
                return '123 (fixture) ' + ' '.join(
                    ['S'] + ['0'] * 18 + ['456'])
            return original(target, *args, **kwargs)

        with patch.object(Path, 'read_text', read):
            with self.assertRaisesRegex(RuntimeError, 'has not exited'):
                runner.helper_cleanup_evidence(self.out)

        def gone(target, *args, **kwargs):
            if target == Path('/proc/123/stat'):
                raise FileNotFoundError('fixture gone')
            return original(target, *args, **kwargs)

        with patch.object(Path, 'read_text', gone):
            result = runner.helper_cleanup_evidence(self.out)
        self.assertTrue(result['started'])
        self.assertFalse(result['live_lock_owner'])

        def denied(target, *args, **kwargs):
            if target == Path('/proc/123/stat'):
                raise PermissionError('fixture permission unknown')
            return original(target, *args, **kwargs)

        with patch.object(Path, 'read_text', denied):
            with self.assertRaises(PermissionError):
                runner.helper_cleanup_evidence(self.out)


if __name__ == '__main__':
    unittest.main()
